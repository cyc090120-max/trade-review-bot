"""Today's fills from the Tradovate web app (FundedNext account), using the login saved by ./login.sh.

Right after loading, the web app asks its websocket for everything (`user/syncrequest`): accounts,
positions, contracts, orders, fills. We listen to that answer instead of scraping the page.
Tradovate only hands out the current trade date's fills (the trade date rolls over at 18:00 ET),
so every day's fills are also kept in data/fills/ for re-runs.
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .session import SITES, LoginExpired, looks_logged_in, open_page, relogin_hint, save_state
from .trades import Fill


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


def fetch_fills(root: Path, day: str, tz: str) -> list[Fill]:
    cache = root / "data" / "fills" / f"{day}.json"
    if day != datetime.now(ZoneInfo(tz)).date().isoformat():
        if cache.exists():
            return _load(cache)
        raise RuntimeError(f"Tradovate 只提供当天的成交，{day} 的成交之前没有保存过，没法补复盘。")
    with open_page(root, "tradovate") as page:
        data = _sync_data(page)
        save_state(page, root, "tradovate")
    fills = _parse(data, day)
    if fills:
        _save(cache, fills)
    elif cache.exists():  # after 18:00 ET Tradovate has already moved on to the next trade date
        return _load(cache)
    return fills
