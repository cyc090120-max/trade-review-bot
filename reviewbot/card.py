"""Render the review card (HTML -> PNG with headless Chromium)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .gemini_review import Review
from .trades import Trade

WEEKDAYS = "一二三四五六日"


def verdict_class(v: str) -> str:
    if any(k in v for k in ("到位", "正确", "好")):
        return "b-good"
    if any(k in v for k in ("不该", "错误", "违背", "差")):
        return "b-bad"
    return "b-mid"


def launch_chromium(p):
    """Use CHROMIUM_PATH if set, otherwise Playwright's own Chromium."""
    path = os.getenv("CHROMIUM_PATH")
    return p.chromium.launch(executable_path=path) if path else p.chromium.launch()


def render_card(root: Path, out_png: Path, *, day: str, trades: list[Trade], stats: dict,
                review: Review, model_name: str, tz: str) -> Path:
    from playwright.sync_api import sync_playwright

    zone = ZoneInfo(tz)
    rows = []
    for t in trades:
        rows.append({
            "n": t.n, "symbol": t.symbol, "direction": t.direction, "max_qty": t.max_qty,
            "entry_avg": t.entry_avg, "exit_avg": t.exit_avg,
            "points_per_contract": t.points_per_contract, "pnl_usd": t.pnl_usd,
            "entry_hm": t.entry_time.astimezone(zone).strftime("%H:%M"),
            "exit_hm": t.exit_time.astimezone(zone).strftime("%H:%M") if t.exit_time else "",
        })

    counts: dict[str, int] = {}
    for c in review.events:
        counts[c.verdict] = counts.get(c.verdict, 0) + 1
    order = {"b-good": 0, "b-mid": 1, "b-bad": 2}
    verdict_counts = sorted(counts.items(), key=lambda kv: order[verdict_class(kv[0])])

    d = datetime.fromisoformat(day)
    env = Environment(loader=FileSystemLoader(root / "templates"), autoescape=select_autoescape(["html", "j2"]))
    html = env.get_template("card.html.j2").render(
        date_label=f"{day} 周{WEEKDAYS[d.weekday()]}",
        trades=rows, stats=stats, review=review, model_name=model_name, verdict_counts=verdict_counts,
        generated_at=datetime.now(ZoneInfo("Pacific/Auckland")).strftime("%Y-%m-%d %H:%M 新西兰时间"),
        verdict_class=verdict_class,
    )
    out_png.parent.mkdir(parents=True, exist_ok=True)
    out_png = out_png.resolve()
    html_path = out_png.with_suffix(".html")
    html_path.write_text(html, encoding="utf-8")

    with sync_playwright() as p:
        browser = launch_chromium(p)
        page = browser.new_page(viewport={"width": 1200, "height": 800}, device_scale_factor=1.5)
        page.goto(html_path.as_uri())
        page.wait_for_load_state("networkidle")
        page.screenshot(path=str(out_png), full_page=True)
        browser.close()
    return out_png
