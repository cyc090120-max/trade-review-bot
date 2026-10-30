"""Fills and bars from the Tradovate web app (FundedNext account), using the login saved by ./login.sh.

Right after loading, the web app asks its websocket for everything (`user/syncrequest`): accounts,
positions, contracts, orders, fills. We listen to that answer instead of scraping the page.
Tradovate only hands out the current trade date's fills (the trade date rolls over at 18:00 ET),
so every day's fills are also kept in data/fills/ for re-runs.

Bars come from the web app's own market-data websocket (`md/getchart`, the same request its chart
makes), for the exact contract traded. That is the data the trader sees, with none of Yahoo's bad
prints or roll-week mismatches.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from .session import SITES, LoginExpired, looks_logged_in, open_page, relogin_hint, save_state
from .market import Bar
from .trades import Fill

# keep a handle on the web app's market-data socket and everything it receives
MD_HOOK = r"""
(() => {
  const Orig = window.WebSocket;
  window.__md = {sock: null, frames: []};
  window.WebSocket = function (url, protos) {
    const ws = protos ? new Orig(url, protos) : new Orig(url);
    if (/\/\/md[-.]/.test(String(url))) {
      window.__md.sock = ws;
      ws.addEventListener('message', (e) => window.__md.frames.push(String(e.data)));
    }
    return ws;
  };
  window.WebSocket.prototype = Orig.prototype;
  Object.assign(window.WebSocket, Orig);
})();
"""
CHART = {"1m": ("MinuteBar", 1, 1380), "5m": ("MinuteBar", 5, 276), "60m": ("MinuteBar", 60, 23),
         "1d": ("DailyBar", 1, 1)}       # interval -> (underlyingType, elementSize, bars per calendar day)
MAX_ELEMENTS = 5000


def _sync_data(page, timeout_s: float = 60) -> dict[str, list]:
    sent: dict[int, str] = {}
    got: dict[str, list] = {}

    def on_ws(ws):
        def on_sent(payload):  # requests look like "endpoint\nid\nquery\nbody"
            parts = payload.split("\n") if isinstance(payload, str) else []
            if len(parts) >= 2 and parts[1].isdigit():
                sent[int(parts[1])] = parts[0]

        def on_received(payload):  # responses look like 'a[{"i": id, "s": 200, "d": {...}}]'
            if not isinstance(payload, str) or not payload.startswith("a["):
                return
            for m in json.loads(payload[1:]):
                if sent.get(m.get("i")) == "user/syncrequest" and isinstance(m.get("d"), dict):
                    for k, v in m["d"].items():  # live and demo each send one; merge them
                        if isinstance(v, list):
                            got.setdefault(k, []).extend(v)

        ws.on("framesent", on_sent)
        ws.on("framereceived", on_received)

    page.on("websocket", on_ws)
    page.goto(SITES["tradovate"]["url"], wait_until="domcontentloaded", timeout=60000)
    deadline = time.monotonic() + timeout_s
    while not got:
        if not looks_logged_in(page, wait_s=0):
            raise LoginExpired(relogin_hint("tradovate"))
        if time.monotonic() > deadline:
            raise RuntimeError(f"Tradovate 网页打开了，但 {timeout_s:.0f} 秒内没有收到账户数据")
        page.wait_for_timeout(1000)
    page.wait_for_timeout(3000)  # a second account environment may answer a moment later
    return got


def _parse(data: dict[str, list], day: str) -> list[Fill]:
    names = {c["id"]: c["name"] for c in data.get("contracts", [])}
    fills = []
    for f in data.get("fills", []):
        td = f.get("tradeDate") or {}
        if f"{td.get('year')}-{td.get('month', 0):02d}-{td.get('day', 0):02d}" != day or f.get("active") is False:
            continue
        if f["contractId"] not in names:
            raise RuntimeError(f"Tradovate 成交里有不认识的合约编号 {f['contractId']}")
        fills.append(Fill(datetime.fromisoformat(f["timestamp"].replace("Z", "+00:00")), names[f["contractId"]],
                          f["action"], int(f["qty"]), float(f["price"])))
    return sorted(fills, key=lambda x: x.time)


def _save(path: Path, fills: list[Fill]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{"time": f.time.isoformat(), "contract": f.contract, "side": f.side, "qty": f.qty, "price": f.price}
            for f in fills]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")


def _load(path: Path) -> list[Fill]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [Fill(datetime.fromisoformat(r["time"]), r["contract"], r["side"], r["qty"], r["price"]) for r in rows]


class Session:
    """One logged-in Tradovate page: the sync data (fills) and bars on request."""

    def __init__(self, page, data: dict[str, list]):
        self.page, self.data, self.next_id = page, data, 90000
        deadline = time.monotonic() + 20
        while not page.evaluate("!!(window.__md && window.__md.sock && window.__md.sock.readyState === 1)"):
            if time.monotonic() > deadline:
                raise RuntimeError("Tradovate 行情连接没有建立")
            page.wait_for_timeout(500)
        page.wait_for_timeout(1500)   # let the app finish authorizing the socket

    def bars(self, contract: str, interval: str, start: date, end: date, zone: ZoneInfo) -> list[Bar]:
        """Bars of `contract` from `start` up to (not including) `end`, ET dates."""
        kind, size, per_day = CHART[interval]
        closest = min(datetime.combine(end, dtime(0), zone), datetime.now(timezone.utc))
        body = {"symbol": contract,
                "chartDescription": {"underlyingType": kind, "elementSize": size,
                                     "elementSizeUnit": "UnderlyingUnits", "withHistogram": False},
                "timeRange": {"closestTimestamp": closest.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                              "asMuchAsElements": min(per_day * (end - start).days + 10, MAX_ELEMENTS)}}
        self.next_id += 1
        rid, page = self.next_id, self.page
        seen = page.evaluate("window.__md.frames.length")
        page.evaluate("([id, b]) => window.__md.sock.send(`md/getchart\n${id}\n\n${JSON.stringify(b)}`)", [rid, body])
        hist_id, charts, done = None, {}, False
        deadline = time.monotonic() + 30
        while not done:
            new = page.evaluate("i => window.__md.frames.slice(i)", seen)
            seen += len(new)
            for f in new:
                if not f.startswith("a["):
                    continue
                for m in json.loads(f[1:]):
                    if m.get("i") == rid:
                        if m.get("s") != 200:
                            raise RuntimeError(f"Tradovate 拒绝了 K 线请求：{m.get('d')}")
                        hist_id = m["d"].get("historicalId")
                    elif m.get("e") == "chart":
                        for c in m["d"].get("charts", []):
                            entry = charts.setdefault(c["id"], {"bars": [], "eoh": False})
                            entry["bars"] += c.get("bars", [])
                            entry["eoh"] |= bool(c.get("eoh"))
            done = hist_id is not None and charts.get(hist_id, {}).get("eoh", False)
            if not done:
                if time.monotonic() > deadline:
                    raise RuntimeError(f"等 Tradovate {contract} {interval} K 线超时")
                page.wait_for_timeout(300)
        page.evaluate("id => window.__md.sock.send(`md/cancelchart\n0\n\n${JSON.stringify({subscriptionId: id})}`)", hist_id)
        out = []
        for b in charts[hist_id]["bars"]:
            if interval == "1d":   # daily bars are stamped with their trade date
                t = datetime.combine(date.fromisoformat(b["timestamp"][:10]), dtime(0), zone)
            else:
                t = datetime.fromisoformat(b["timestamp"].replace("Z", "+00:00")).astimezone(zone)
            if t >= datetime.combine(start, dtime(0), zone):
                out.append(Bar(t, float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"]),
                               float(b.get("upVolume", 0) + b.get("downVolume", 0))))
        return sorted(out, key=lambda x: x.t)


@contextmanager
def session(root: Path, optional: bool = False):
    """A logged-in Tradovate page for the whole fetch. optional=True yields None instead of failing."""
    try:
        cm = open_page(root, "tradovate")
        page = cm.__enter__()
    except Exception:
        if not optional:
            raise
        yield None
        return
    try:
        try:
            page.add_init_script(script=MD_HOOK)
            tv = Session(page, _sync_data(page))
        except Exception:
            if not optional:
                raise
            tv = None
        yield tv
        if tv:
            save_state(page, root, "tradovate")
    finally:
        cm.__exit__(None, None, None)


def has_cache(root: Path, day: str, tz: str) -> bool:
    return day != datetime.now(ZoneInfo(tz)).date().isoformat() and (root / "data" / "fills" / f"{day}.json").exists()


def fetch_fills(root: Path, day: str, tz: str, tv: Session | None) -> list[Fill]:
    cache = root / "data" / "fills" / f"{day}.json"
    if day != datetime.now(ZoneInfo(tz)).date().isoformat():
        if cache.exists():
            return _load(cache)
        raise RuntimeError(f"Tradovate 只提供当天的成交，{day} 的成交之前没有保存过，没法补复盘。")
    if tv is None:
        raise RuntimeError("Tradovate 没有打开")
    fills = _parse(tv.data, day)
    if fills:
        _save(cache, fills)
    elif cache.exists():  # after 18:00 ET Tradovate has already moved on to the next trade date
        return _load(cache)
    return fills
