"""Charts: the day's 5-minute overview, the 1-hour background before the open, and one 1-minute
close-up per trade with that trade's comments underneath (big type, readable on a phone)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .card import WEEKDAYS, launch_chromium, verdict_class
from .context import visible_zones
from .events import Event
from .gemini_review import Review
from .market import DayBars, wall_epoch
from .trades import Trade

LEVEL_COLORS = [("前周", "#e91e63"), ("前日", "#f7a600"), ("隔夜", "#9c6ade"), ("亚盘", "#9c6ade"), ("伦敦", "#9c6ade"),
                ("等高", "#ffeb3b"), ("等低", "#ffeb3b"), ("日线", "#26c6da"), ("4H", "#26c6da"), ("1H", "#4dd0e1"),
                ("15m", "#80cbc4"), ("整数", "#6b6f7d"), ("SPX", "#6b6f7d"), ("NDX", "#6b6f7d")]
VCLASS = {"b-good": "good", "b-mid": "mid", "b-bad": "bad"}


# lower = more important; a line several sources agree on ranks higher still
LEVEL_RANK = [("前周", 0), ("前日", 1), ("隔夜", 2), ("日线", 2), ("4H", 3), ("亚盘", 3), ("伦敦", 3),
              ("1H", 4), ("等高", 4), ("等低", 4), ("SPX", 5), ("NDX", 5), ("整数", 5), ("15m", 6)]
HTF_LEVELS = ("前周", "前日", "隔夜", "日线", "4H", "1H")
MAX_LEVELS, MAX_ZONES = 7, 3
# fills: "arrow" = 买/卖 tags with arrows, "box" = one green/red box per trade, "both". Boxes overlap on a
# busy day, so the overview uses arrows only and each trade close-up gets both.
OVERVIEW_MODE, TRADE_MODE = "arrow", "both"


def _rank(name: str) -> float:
    parts = name.split(" / ")
    best = min(next((r for k, r in LEVEL_RANK if k in part), 9) for part in parts)
    return best - 0.5 * (len(parts) - 1)


def _levels(db: DayBars, bars: list[dict], only: tuple[str, ...] | None = None) -> tuple[list[dict], list[float]]:
    """At most MAX_LEVELS of the most important levels in (or just outside) the visible range; lines too
    close to tell apart are merged. The price scale stretches to show them."""
    lo, hi = min(b["low"] for b in bars), max(b["high"] for b in bars)
    pad = (hi - lo) * 0.15
    cands = [(n, p) for n, p in db.levels.items()
             if lo - pad <= p <= hi + pad and (only is None or any(k in n for k in only))]
    if only:  # keep only the allowed parts of merged names
        cands = [(" / ".join(x for x in n.split(" / ") if any(k in x for k in only)), p) for n, p in cands]
    chosen = sorted(sorted(cands, key=lambda x: _rank(x[0]))[:MAX_LEVELS], key=lambda x: x[1])
    merged: list[list] = []
    gap = (hi - lo + 2 * pad) * 0.025
    for n, p in chosen:
        if merged and p - merged[-1][1] < gap:
            merged[-1][0] += " / " + n
            if _rank(n) < _rank(merged[-1][0].split(" / ")[0]):
                merged[-1][1] = p
        else:
            merged.append([n, p])
    out = []
    for n, p in merged:
        head = min(n.split(" / "), key=_rank)
        color = next((c for k, c in LEVEL_COLORS if k in head), "#787b86")
        out.append({"name": head + (" 等" if " / " in n else ""), "price": p, "color": color,
                    "width": 2 if " / " in n else 1})
    return out, [l["price"] for l in out]


def _zones(zones: list[dict], bars: list[dict], ref: float, tfs: tuple[str, ...] | None = None) -> list[dict]:
    """The MAX_ZONES FVGs / OBs / gaps closest to `ref`, drawn from where they formed to the right edge."""
    lo, hi = min(b["low"] for b in bars), max(b["high"] for b in bars)
    times = [b["time"] for b in bars]
    cands = []
    for z in zones:
        if z["hi"] < lo or z["lo"] > hi or z["start"] > times[-1] or (tfs is not None and z.get("tf", "") not in tfs):
            continue
        if z["hi"] - z["lo"] > (hi - lo) * 0.35:  # a box covering most of the chart hides more than it shows
            continue
        dist = 0 if z["lo"] <= ref <= z["hi"] else min(abs(z["lo"] - ref), abs(z["hi"] - ref))
        start = next(t for t in times if t >= z["start"]) if z["start"] >= times[0] else times[0]
        cands.append((dist, {**z, "start": start}))
    return [z for _, z in sorted(cands, key=lambda x: x[0])[:MAX_ZONES]]


def _boxes(trades: list[Trade], bars: list[dict], step: int, tz: str) -> list[dict]:
    """One box per closed trade: entry time/avg price to exit time/avg price."""
    times = [b["time"] for b in bars]
    snap = lambda t: min(times, key=lambda x: abs(x - (t - t % step)))
    out = []
    for t in trades:
        if t.is_open:
            continue
        t0, t1 = wall_epoch(t.entry_time, tz), wall_epoch(t.exit_time, tz)
        if t1 < times[0] or t0 > times[-1] + step:
            continue
        out.append({"t0": snap(t0), "t1": snap(t1), "p0": t.entry_avg, "p1": t.exit_avg, "pnl": t.pnl_usd,
                    "label": f"第{t.n}笔 {t.points_per_contract:+.2f}"})
    return out


def _markers(events: list[Event], bars: list[dict], step: int, review: Review | None, tz: str) -> list[dict]:
    comments = {c.n: c for c in (review.events if review else [])}
    times = [b["time"] for b in bars]
    out = []
    for e in events:
        t = wall_epoch(e.time, tz)
        t -= t % step
        if not times or t < times[0] - step or t > times[-1] + step:
            continue
        c = comments.get(e.n)
        out.append({"n": e.n, "is_buy": e.is_buy, "price": e.price, "side_text": "买" if e.is_buy else "卖", "qty": e.qty,
                    "bar_time": t if t in times else min(times, key=lambda x: abs(x - t)),
                    "verdict_class": VCLASS[verdict_class(c.verdict)] if c else ""})
    return out


def _render(root: Path, out_png: Path, *, width: int, chart_height: int, title: str, sub: str,
            bars: list[dict], db: DayBars, vwap: list[dict], markers: list[dict], items: list[dict],
            zones: list[dict], level_kinds: tuple[str, ...] | None = None, boxes: list[dict] | None = None,
            mode: str = OVERVIEW_MODE) -> Path:
    from playwright.sync_api import sync_playwright

    levels, extra = _levels(db, bars, level_kinds)
    env = Environment(loader=FileSystemLoader(root / "templates"), autoescape=select_autoescape(["html", "j2"]))
    page_html = env.get_template("chart.html.j2").render(
        width=width, chart_height=chart_height, title=title, sub=sub, items=items,
        lib_uri=(root / "reviewbot/vendor/lightweight-charts.standalone.production.js").as_uri(),
        bars_json=json.dumps(bars), vwap_json=json.dumps(vwap), levels_json=json.dumps(levels, ensure_ascii=False),
        scale_extra_json=json.dumps(extra), markers_json=json.dumps(markers),
        zones_json=json.dumps(zones, ensure_ascii=False), trades_json=json.dumps(boxes or [], ensure_ascii=False),
        mode=mode,
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_png = out_png.resolve()
    html_path = out_png.with_suffix(".html")
    html_path.write_text(page_html, encoding="utf-8")
    with sync_playwright() as p:
        browser = launch_chromium(p)
        page = browser.new_page(viewport={"width": width, "height": 800}, device_scale_factor=2)
        page.goto(html_path.as_uri())
        page.wait_for_function("window.__ready === true", timeout=20000)
        page.screenshot(path=str(out_png), full_page=True)
        browser.close()
    return out_png


def _date_label(day: str) -> str:
    return f"{day} 周{WEEKDAYS[datetime.fromisoformat(day).weekday()]}"


def _pnl(usd: float) -> str:
    return f'<b class="{"up" if usd > 0 else "down"}">{usd:+,.2f}</b>'


def render_overview(root: Path, out_png: Path, *, day: str, db: DayBars, events: list[Event],
                    trades: list[Trade], tz: str, mode: str = OVERVIEW_MODE) -> Path:
    closed = [t for t in trades if t.symbol == db.symbol and not t.is_open]
    sub = (f"{_date_label(day)} · 盘中 09:30–16:00 ET · 白线 VWAP · 绿红 FVG · 蓝橙 OB · 紫色缺口 · "
           f"{len(closed)} 笔合计 {_pnl(sum(t.pnl_usd for t in closed))} 美元")
    vwap5 = [v for v in db.vwap if v["time"] % 300 == 240]  # VWAP at the close of each 5-minute bar
    vwap5 = [{"time": v["time"] - 240, "value": v["value"]} for v in vwap5]
    return _render(root, out_png, width=1200, chart_height=640, title=f"{db.symbol} 全天 5 分钟", sub=sub,
                   bars=db.bars, db=db, vwap=vwap5,
                   markers=_markers([e for e in events if e.symbol == db.symbol], db.bars, 300, None, tz), items=[],
                   zones=_zones(db.zones, db.bars, db.bars[0]["open"]),
                   boxes=_boxes([t for t in trades if t.symbol == db.symbol], db.bars, 300, tz), mode=mode)


def render_htf(root: Path, out_png: Path, *, day: str, db: DayBars) -> Path:
    return _render(root, out_png, width=1200, chart_height=640, title=f"{db.symbol} 1 小时 · 开盘前的大周期",
                   sub=f"最近 5 个交易日，截至 {_date_label(day)} 09:30 ET · 虚线是开盘前已知的关键位",
                   bars=db.htf, db=db, vwap=[], markers=[], items=[],
                   zones=_zones(db.zones, db.htf, db.htf[-1]["close"], tfs=("1H", "4H", "")), level_kinds=HTF_LEVELS)


def render_trade(root: Path, out_png: Path, *, day: str, db: DayBars, trade: Trade, events: list[Event],
                 review: Review | None, tz: str, mode: str = TRADE_MODE) -> Path:
    zone = ZoneInfo(tz)
    evs = [e for e in events if e.trade_n == trade.n]
    end = trade.exit_time or evs[-1].time
    t0 = wall_epoch(trade.entry_time - timedelta(minutes=20), tz)
    t1 = wall_epoch(end + timedelta(minutes=10), tz)
    one_min = [b for b in db.bars1 if t0 <= b["time"] <= t1]
    if one_min and len(one_min) <= 150:
        bars, step, vwap, tf = one_min, 60, [v for v in db.vwap if t0 <= v["time"] <= t1], "1 分钟"
    else:  # no 1-minute data, or a long hold: 5-minute bars with more room around the trade
        t0, t1 = t0 - 40 * 60, t1 + 20 * 60
        bars, step, tf = [b for b in db.bars if t0 <= b["time"] <= t1], 300, "5 分钟"
        vwap = [{"time": v["time"] - 240, "value": v["value"]} for v in db.vwap
                if v["time"] % 300 == 240 and t0 <= v["time"] - 240 <= t1]
    side = "做多" if trade.direction == "long" else "做空"
    hm = lambda d: d.astimezone(zone).strftime("%H:%M")
    title = f"第{trade.n}笔 · {trade.symbol} {side} {trade.max_qty}张 · {hm(trade.entry_time)}–{hm(end)}"
    sub = (f"{tf} · 每张 {trade.points_per_contract:+.2f} 点 · {_pnl(trade.pnl_usd)} 美元"
           if not trade.is_open else f"{tf} · 收盘时仍持仓")
    comments = {c.n: c for c in (review.events if review else [])}
    items = []
    for e in evs:
        c = comments.get(e.n)
        if c:
            items.append({"label": "买" if e.is_buy else "卖", "side": "buy" if e.is_buy else "sell", "time": e.time.astimezone(zone).strftime("%H:%M:%S"), "title": e.title,
                          "action": f"{'买' if e.is_buy else '卖'}{e.qty} @ {e.price:,.2f}",
                          "verdict": c.verdict, "vclass": VCLASS[verdict_class(c.verdict)], "tag": c.tag,
                          "comment": c.comment, "hindsight": c.hindsight})
    return _render(root, out_png, width=900, chart_height=520, title=title, sub=sub, bars=bars, db=db, vwap=vwap,
                   markers=_markers(evs, bars, step, review, tz), items=items,
                   zones=_zones(visible_zones(db, wall_epoch(end, tz), t0 if step == 60 else None), bars, evs[0].price),
                   boxes=_boxes([trade], bars, step, tz), mode=mode)
