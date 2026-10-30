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


def _levels(db: DayBars, bars: list[dict]) -> tuple[list[dict], list[float]]:
    """Levels inside (or just outside) the visible price range; the scale stretches to show them."""
    lo, hi = min(b["low"] for b in bars), max(b["high"] for b in bars)
    pad = (hi - lo) * 0.2
    out = []
    for name, p in db.levels.items():
        if lo - pad <= p <= hi + pad:
            color = next((c for k, c in LEVEL_COLORS if k in name), "#787b86")
            out.append({"name": name.split(" / ")[0] + (" 等" if " / " in name else ""), "price": p,
                        "color": color, "width": 2 if " / " in name else 1})
    return out, [l["price"] for l in out]


def _zones(zones: list[dict], bars: list[dict]) -> list[dict]:
    """FVGs / gaps overlapping the visible range, drawn from where they formed to the right edge."""
    lo, hi = min(b["low"] for b in bars), max(b["high"] for b in bars)
    times = [b["time"] for b in bars]
    out = []
    for z in zones:
        if z["hi"] < lo or z["lo"] > hi or z["start"] > times[-1]:
            continue
        if z["hi"] - z["lo"] > (hi - lo) * 0.35:  # a box covering most of the chart hides more than it shows
            continue
        start = next(t for t in times if t >= z["start"]) if z["start"] >= times[0] else times[0]
        out.append({**z, "start": start})
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
        out.append({"n": e.n, "is_buy": e.is_buy, "price": e.price,
                    "bar_time": t if t in times else min(times, key=lambda x: abs(x - t)),
                    "verdict_class": VCLASS[verdict_class(c.verdict)] if c else ""})
    return out


def _render(root: Path, out_png: Path, *, width: int, chart_height: int, title: str, sub: str,
            bars: list[dict], db: DayBars, vwap: list[dict], markers: list[dict], items: list[dict],
            zones: list[dict]) -> Path:
    from playwright.sync_api import sync_playwright

    levels, extra = _levels(db, bars)
    env = Environment(loader=FileSystemLoader(root / "templates"), autoescape=select_autoescape(["html", "j2"]))
    page_html = env.get_template("chart.html.j2").render(
        width=width, chart_height=chart_height, title=title, sub=sub, items=items,
        lib_uri=(root / "reviewbot/vendor/lightweight-charts.standalone.production.js").as_uri(),
        bars_json=json.dumps(bars), vwap_json=json.dumps(vwap), levels_json=json.dumps(levels, ensure_ascii=False),
        scale_extra_json=json.dumps(extra), markers_json=json.dumps(markers),
        zones_json=json.dumps(_zones(zones, bars), ensure_ascii=False),
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
                    trades: list[Trade], tz: str) -> Path:
    closed = [t for t in trades if t.symbol == db.symbol and not t.is_open]
    sub = (f"{_date_label(day)} · 盘中 09:30–16:00 ET · 白线 VWAP · 色块是 FVG / 缺口 · "
           f"{len(closed)} 笔合计 {_pnl(sum(t.pnl_usd for t in closed))} 美元")
    vwap5 = [v for v in db.vwap if v["time"] % 300 == 240]  # VWAP at the close of each 5-minute bar
    vwap5 = [{"time": v["time"] - 240, "value": v["value"]} for v in vwap5]
    return _render(root, out_png, width=1200, chart_height=640, title=f"{db.symbol} 全天 5 分钟", sub=sub,
                   bars=db.bars, db=db, vwap=vwap5,
                   markers=_markers([e for e in events if e.symbol == db.symbol], db.bars, 300, None, tz), items=[],
                   zones=db.zones)


def render_htf(root: Path, out_png: Path, *, day: str, db: DayBars) -> Path:
    return _render(root, out_png, width=1200, chart_height=640, title=f"{db.symbol} 1 小时 · 开盘前的大周期",
                   sub=f"最近 5 个交易日，截至 {_date_label(day)} 09:30 ET · 虚线是开盘前已知的关键位",
                   bars=db.htf, db=db, vwap=[], markers=[], items=[], zones=db.zones)


def render_trade(root: Path, out_png: Path, *, day: str, db: DayBars, trade: Trade, events: list[Event],
                 review: Review | None, tz: str) -> Path:
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
            items.append({"label": e.label, "time": e.time.astimezone(zone).strftime("%H:%M:%S"), "title": e.title,
                          "action": f"{'买' if e.is_buy else '卖'}{e.qty} @ {e.price:,.2f}",
                          "verdict": c.verdict, "vclass": VCLASS[verdict_class(c.verdict)], "tag": c.tag,
                          "comment": c.comment, "hindsight": c.hindsight})
    return _render(root, out_png, width=900, chart_height=520, title=title, sub=sub, bars=bars, db=db, vwap=vwap,
                   markers=_markers(evs, bars, step, review, tz), items=items,
                   zones=visible_zones(db, wall_epoch(end, tz)))
