"""Charts, all drawn the same way as the trader's own 5-minute chart (white, hollow green up candles,
solid red down, volume underneath): the day's overview, the 1-hour background before the open, and
zoomed-in views of that same 5-minute chart for each trade and each teaching example, with the text
underneath (big type, readable on a phone)."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from .card import WEEKDAYS, launch_chromium, verdict_class
from .context import visible_zones
from .events import Event
from .gemini_review import Lesson, Review
from .market import DayBars, wall_epoch
from .orders import TradeRisk
from .trades import Trade

LEVEL_COLORS = [("前周", "#d81b60"), ("前日", "#ef6c00"), ("隔夜", "#7e57c2"), ("亚盘", "#7e57c2"), ("伦敦", "#7e57c2"),
                ("等高", "#c0a000"), ("等低", "#c0a000"), ("日线", "#00838f"), ("4H", "#00838f"), ("1H", "#0097a7"),
                ("15m", "#26a69a"), ("整数", "#787b86"), ("SPX", "#787b86"), ("NDX", "#787b86")]
VCLASS = {"b-good": "good", "b-mid": "mid", "b-bad": "bad"}

# lower = more important; a line several sources agree on ranks higher still
LEVEL_RANK = [("前周", 0), ("前日", 1), ("隔夜", 2), ("日线", 2), ("4H", 3), ("亚盘", 3), ("伦敦", 3),
              ("1H", 4), ("等高", 4), ("等低", 4), ("SPX", 5), ("NDX", 5), ("整数", 5), ("15m", 6)]
HTF_LEVELS = ("前周", "前日", "隔夜", "日线", "4H", "1H")
MAX_LEVELS, MAX_ZONES = 7, 3
# fills: "arrow" = 买/卖 tags with arrows, "both" = tags plus a green/red box per trade. Boxes overlap
# on a busy day, so the overview uses arrows only.
OVERVIEW_MODE, ZOOM_MODE = "arrow", "both"
WIDE, ZOOM = (1200, 960), (1000, 800)       # width, chart height: about the trader's own chart's 5:4
ZOOM_PAD_BARS, ZOOM_MIN_BARS = 12, 36       # 5-minute bars shown around a trade / lesson


def _rank(name: str) -> float:
    parts = name.split(" / ")
    return min(next((r for k, r in LEVEL_RANK if k in part), 9) for part in parts) - 0.5 * (len(parts) - 1)


def _levels(db: DayBars, bars: list[dict], only: tuple[str, ...] | None = None) -> list[dict]:
    """At most MAX_LEVELS of the most important levels inside the price range of the bars shown (the
    scale is not stretched for far-away levels, so candles keep their shape); close lines merge."""
    lo, hi = min(b["low"] for b in bars), max(b["high"] for b in bars)
    pad = (hi - lo) * 0.03
    cands = [(n, p) for n, p in db.levels.items()
             if lo - pad <= p <= hi + pad and (only is None or any(k in n for k in only))]
    if only:  # keep only the allowed parts of merged names
        cands = [(" / ".join(x for x in n.split(" / ") if any(k in x for k in only)), p) for n, p in cands]
    chosen = sorted(sorted(cands, key=lambda x: _rank(x[0]))[:MAX_LEVELS], key=lambda x: x[1])
    merged: list[list] = []
    for n, p in chosen:
        if merged and p - merged[-1][1] < (hi - lo) * 0.025:
            merged[-1][0] += " / " + n
        else:
            merged.append([n, p])
    out = []
    for n, p in merged:
        head = min(n.split(" / "), key=_rank)
        out.append({"name": head + (" 等" if " / " in n else ""), "price": p,
                    "color": next((c for k, c in LEVEL_COLORS if k in head), "#787b86"),
                    "width": 2 if " / " in n else 1})
    return out


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


def _snap(times: list[int], t: int, step: int) -> int:
    return min(times, key=lambda x: abs(x - (t - t % step)))


def _boxes(trades: list[Trade], bars: list[dict], step: int, tz: str) -> list[dict]:
    """One box per closed trade: entry time/avg price to exit time/avg price."""
    times = [b["time"] for b in bars]
    out = []
    for t in trades:
        if t.is_open:
            continue
        t0, t1 = wall_epoch(t.entry_time, tz), wall_epoch(t.exit_time, tz)
        if t1 < times[0] or t0 > times[-1] + step:
            continue
        out.append({"t0": _snap(times, t0, step), "t1": _snap(times, t1, step), "p0": t.entry_avg,
                    "p1": t.exit_avg, "pnl": t.pnl_usd, "label": f"第{t.n}笔 {t.points_per_contract:+.2f}"})
    return out


def _risk_lines(trade: Trade, risk: TradeRisk | None, bars: list[dict], tz: str) -> list[dict]:
    """Stop / target prices over time as flat segments (a moved stop becomes steps), snapped to the 5-minute
    bars: several moves inside one bar keep only the last price, and only the first and last segment of each
    order carry a price label."""
    if not risk:
        return []
    times = [b["time"] for b in bars]
    end_trade = trade.exit_time or trade.entry_time
    out, seen = [], set()
    for leg in risk.stops + risk.targets:
        stop_at = min(x for x in (leg.end, end_trade) if x is not None)
        per_bar: dict[int, float] = {}
        for w, p in leg.steps:
            if w <= stop_at:
                per_bar[_snap(times, wall_epoch(max(w, trade.entry_time), tz), 300)] = p
        starts = sorted(per_bar)
        end_bar = _snap(times, wall_epoch(stop_at, tz), 300)
        word = "止损" if leg.kind == "stop" else "止盈"
        for i, t0 in enumerate(starts):
            t1 = starts[i + 1] if i + 1 < len(starts) else end_bar
            key = (leg.kind, t0, t1, per_bar[t0])
            if key in seen:
                continue
            seen.add(key)
            label = f"{word} {per_bar[t0]:.2f}" if i == 0 else (f"→ {per_bar[t0]:.2f}" if i == len(starts) - 1 else "")
            out.append({"t0": t0, "t1": t1, "price": per_bar[t0], "kind": leg.kind, "label": label})
    return out


def _markers(events: list[Event], bars: list[dict], step: int, review: Review | None, tz: str) -> list[dict]:
    comments = {c.n: c for c in (review.events if review else [])}
    times = [b["time"] for b in bars]
    out = []
    for e in events:
        t = wall_epoch(e.time, tz)
        if not times or t < times[0] or t >= times[-1] + step:
            continue
        c = comments.get(e.n)
        out.append({"is_buy": e.is_buy, "price": e.price, "side_text": "买" if e.is_buy else "卖", "qty": e.qty,
                    "bar_time": _snap(times, t, step),
                    "verdict_class": VCLASS[verdict_class(c.verdict)] if c else ""})
    return out


def _vwap5(db: DayBars, bars: list[dict]) -> list[dict]:
    """VWAP at the close of each 5-minute bar, drawn at the bar's time."""
    t0, t1 = bars[0]["time"], bars[-1]["time"]
    return [{"time": v["time"] - 240, "value": v["value"]} for v in db.vwap
            if v["time"] % 300 == 240 and t0 <= v["time"] - 240 <= t1]


def _window(db: DayBars, t0: int, t1: int) -> list[dict]:
    """5-minute bars from t0 to t1 plus ZOOM_PAD_BARS each side, widened to at least ZOOM_MIN_BARS."""
    times = [b["time"] for b in db.bars]
    i0 = max(0, next((i for i, t in enumerate(times) if t + 300 > t0), 0) - ZOOM_PAD_BARS)
    i1 = min(len(times), next((i for i, t in enumerate(times) if t > t1), len(times)) + ZOOM_PAD_BARS)
    while i1 - i0 < ZOOM_MIN_BARS and (i0 > 0 or i1 < len(times)):
        i0, i1 = max(0, i0 - 1), min(len(times), i1 + 1)
    return db.bars[i0:i1]


def _render(root: Path, out_png: Path, *, size: tuple[int, int], title: str, sub: str, bars: list[dict],
            db: DayBars, vwap: list[dict], markers: list[dict], items: list[dict], zones: list[dict],
            level_kinds: tuple[str, ...] | None = None, boxes: list[dict] | None = None,
            mode: str = OVERVIEW_MODE, highlight: dict | None = None, extra_levels: list[dict] | None = None,
            risk_lines: list[dict] | None = None) -> Path:
    from playwright.sync_api import sync_playwright

    width, chart_height = size
    env = Environment(loader=FileSystemLoader(root / "templates"), autoescape=select_autoescape(["html", "j2"]))
    page_html = env.get_template("chart.html.j2").render(
        width=width, chart_height=chart_height, title=title, sub=sub, items=items, mode=mode,
        lib_uri=(root / "reviewbot/vendor/lightweight-charts.standalone.production.js").as_uri(),
        bars_json=json.dumps(bars), vwap_json=json.dumps(vwap), markers_json=json.dumps(markers),
        levels_json=json.dumps([l for l in _levels(db, bars, level_kinds)   # a lesson's own line replaces a same-price level
                                if all(abs(l["price"] - x["price"]) > 0.5 for x in extra_levels or [])]
                               + (extra_levels or []), ensure_ascii=False),
        zones_json=json.dumps(zones, ensure_ascii=False), trades_json=json.dumps(boxes or [], ensure_ascii=False),
        highlight_json=json.dumps(highlight), risk_json=json.dumps(risk_lines or [], ensure_ascii=False),
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
    sub = (f"{_date_label(day)} · 盘中 09:30–16:00 ET · 灰线 VWAP · 绿红 FVG · 蓝橙 OB · 紫色缺口 · "
           f"{len(closed)} 笔合计 {_pnl(sum(t.pnl_usd for t in closed))} 美元")
    return _render(root, out_png, size=WIDE, title=f"{db.symbol} 全天 5 分钟", sub=sub, bars=db.bars, db=db,
                   vwap=_vwap5(db, db.bars), items=[],
                   markers=_markers([e for e in events if e.symbol == db.symbol], db.bars, 300, None, tz),
                   zones=_zones(db.zones, db.bars, db.bars[0]["open"]), mode=OVERVIEW_MODE)


def render_htf(root: Path, out_png: Path, *, day: str, db: DayBars) -> Path:
    return _render(root, out_png, size=WIDE, title=f"{db.symbol} 1 小时 · 开盘前的大周期",
                   sub=f"最近 5 个交易日，截至 {_date_label(day)} 09:30 ET · 虚线是开盘前已知的关键位",
                   bars=db.htf, db=db, vwap=[], markers=[], items=[], level_kinds=HTF_LEVELS,
                   zones=_zones(db.zones, db.htf, db.htf[-1]["close"], tfs=("1H", "4H", "")))


def render_trade(root: Path, out_png: Path, *, day: str, db: DayBars, trade: Trade, events: list[Event],
                 review: Review | None, tz: str, risk: TradeRisk | None = None) -> Path:
    """The day's 5-minute chart zoomed in on one trade, with its comments underneath."""
    zone = ZoneInfo(tz)
    evs = [e for e in events if e.trade_n == trade.n]
    end = trade.exit_time or evs[-1].time
    bars = _window(db, wall_epoch(trade.entry_time, tz), wall_epoch(end, tz))
    side = "做多" if trade.direction == "long" else "做空"
    hm = lambda d: d.astimezone(zone).strftime("%H:%M")
    title = f"第{trade.n}笔 · {trade.symbol} {side} {trade.max_qty}张 · {hm(trade.entry_time)}–{hm(end)}"
    sub = (f"5 分钟 · 每张 {trade.points_per_contract:+.2f} 点 · {_pnl(trade.pnl_usd)} 美元"
           if not trade.is_open else "5 分钟 · 收盘时仍持仓")
    comments = {c.n: c for c in (review.events if review else [])}
    items = []
    for e in evs:
        c = comments.get(e.n)
        if c:
            items.append({"label": "买" if e.is_buy else "卖", "side": "buy" if e.is_buy else "sell",
                          "meta": f"<strong>{e.time.astimezone(zone):%H:%M:%S} {e.title}</strong>"
                                  f"{'买' if e.is_buy else '卖'}{e.qty} @ {e.price:,.2f}",
                          "verdict": c.verdict, "vclass": VCLASS[verdict_class(c.verdict)], "tag": c.tag,
                          "comment": c.comment, "hindsight": c.hindsight})
    return _render(root, out_png, size=ZOOM, title=title, sub=sub, bars=bars, db=db, vwap=_vwap5(db, bars),
                   markers=_markers(evs, bars, 300, review, tz), items=items, mode=ZOOM_MODE,
                   zones=_zones(visible_zones(db, wall_epoch(end, tz)), bars, evs[0].price),
                   boxes=_boxes([trade], bars, 300, tz), risk_lines=_risk_lines(trade, risk, bars, tz))


def render_lesson(root: Path, out_png: Path, *, day: str, db: DayBars, lesson: Lesson, k: int,
                  events: list[Event], tz: str) -> Path:
    """A teaching example on the same 5-minute chart: the bars it is about shaded, its key prices drawn."""
    zone = ZoneInfo(tz)
    d = datetime.fromisoformat(day).date()
    at = lambda hm: wall_epoch(datetime.combine(d, time.fromisoformat(hm), zone), tz)
    t0, t1 = sorted((at(lesson.start), at(lesson.end)))
    bars = _window(db, t0, t1)
    times = [b["time"] for b in bars]
    lines = [{"name": lv.name, "price": lv.price, "color": "#7e57c2", "width": 2, "style": 0}
             for lv in lesson.levels if bars and min(b["low"] for b in bars) <= lv.price <= max(b["high"] for b in bars)]
    item = {"label": f"教学 {k}", "side": "lesson", "meta": f"<strong>{lesson.concept}</strong>{lesson.start}–{lesson.end}",
            "tag": lesson.title, "comment": lesson.text}
    return _render(root, out_png, size=ZOOM, title=f"今日教学 {k} · {lesson.title}",
                   sub=f"{_date_label(day)} · {db.symbol} 5 分钟 · 紫色底色是讲解的这几根 K 线",
                   bars=bars, db=db, vwap=_vwap5(db, bars), items=[item], mode="arrow",
                   markers=_markers([e for e in events if e.symbol == db.symbol
                                     and t0 <= wall_epoch(e.time, tz) < t1 + 300], bars, 300, None, tz),
                   zones=_zones(visible_zones(db, t1 + 300), bars, (lesson.levels[0].price if lesson.levels
                                                                    else bars[len(bars) // 2]["close"])),
                   highlight={"t0": _snap(times, t0, 300), "t1": _snap(times, t1, 300)}, extra_levels=lines)
