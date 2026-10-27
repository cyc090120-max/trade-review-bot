"""Draw the 5-minute chart with every entry/exit numbered, pointed at, and (after review) commented."""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .card import WEEKDAYS, launch_chromium, verdict_class
from .events import Event
from .gemini_review import Review
from .market import DayBars, wall_epoch
from .trades import Trade

LEVEL_COLORS = {"PDH": "#f7a600", "PDL": "#f7a600", "ONH": "#9c6ade", "ONL": "#9c6ade"}
VCLASS = {"b-good": "good", "b-mid": "mid", "b-bad": "bad"}
CHART_HEIGHT = 780


def _bar_time(ev: Event, bars: list[dict], tz: str) -> int:
    t = wall_epoch(ev.time, tz)
    t -= t % 300                                           # start of its 5-minute bar
    times = [b["time"] for b in bars]
    return t if t in times else min(times, key=lambda x: abs(x - t))


def render_chart(root: Path, out_png: Path, *, day: str, day_bars: DayBars, events: list[Event],
                 trades: list[Trade], review: Review | None, tz: str) -> Path:
    from playwright.sync_api import sync_playwright

    zone = ZoneInfo(tz)
    comments = {c.n: c for c in (review.events if review else [])}
    ev_json, items = [], []
    for e in events:
        c = comments.get(e.n)
        act = f"{'买' if e.is_buy else '卖'}{e.qty} @ {e.price:,.2f}"
        hm = e.time.astimezone(zone).strftime("%H:%M")
        vc = VCLASS[verdict_class(c.verdict)] if c else ""
        ev_json.append({
            "n": e.n, "label": e.label, "is_buy": e.is_buy, "price": e.price,
            "bar_time": _bar_time(e, day_bars.bars, tz),
            "line1": html.escape(f"{hm} {e.title} · {act}"),
            "verdict": html.escape(c.verdict) if c else "", "verdict_class": vc,
            "tag": html.escape(c.tag) if c else "",
        })
        if c:
            items.append({"label": e.label, "time": hm, "title": e.title, "action": f"{act}（第{e.trade_n}笔）",
                          "verdict": c.verdict, "vclass": vc, "comment": c.comment})

    lo = min(b["low"] for b in day_bars.bars)
    hi = max(b["high"] for b in day_bars.bars)
    near = (hi - lo) * 0.35                    # only stretch the scale for levels that are close by
    levels = [{"name": k, "price": v, "color": next((c for s, c in LEVEL_COLORS.items() if s in k), "#787b86")}
              for k, v in day_bars.levels.items()]
    scale_extra = [l["price"] for l in levels if lo - near <= l["price"] <= hi + near]

    sym_trades = [t for t in trades if t.symbol == day_bars.symbol]
    d = datetime.fromisoformat(day)
    env = Environment(loader=FileSystemLoader(root / "templates"), autoescape=select_autoescape(["html", "j2"]))
    page_html = env.get_template("chart.html.j2").render(
        symbol=day_bars.symbol, date_label=f"{day} 周{WEEKDAYS[d.weekday()]}", height=CHART_HEIGHT,
        trade_pnls=[{"n": t.n, "usd": t.pnl_usd} for t in sym_trades if not t.is_open],
        total=round(sum(t.pnl_usd for t in sym_trades if not t.is_open), 2),
        items=items,
        lib_uri=(root / "reviewbot/vendor/lightweight-charts.standalone.production.js").as_uri(),
        bars_json=json.dumps(day_bars.bars), levels_json=json.dumps(levels, ensure_ascii=False),
        scale_extra_json=json.dumps(scale_extra), events_json=json.dumps(ev_json, ensure_ascii=False),
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_png = out_png.resolve()
    html_path = out_png.with_suffix(".html")
    html_path.write_text(page_html, encoding="utf-8")

    with sync_playwright() as p:
        browser = launch_chromium(p)
        page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=1.5)
        page.goto(html_path.as_uri())
        page.wait_for_function("window.__ready === true", timeout=20000)
        page.screenshot(path=str(out_png), full_page=True)
        browser.close()
    return out_png
