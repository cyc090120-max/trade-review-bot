"""Free market data (Yahoo Finance via yfinance): the day's 5- and 1-minute bars, the 1-hour bars
before the open, and the levels the trader watches that are already known before the open:
previous day / week high-low, overnight / Asia / London high-low, unbroken swing highs and lows on
15m / 1H / 4H / daily, the opening gap and round numbers."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

YAHOO = {"MNQ": "MNQ=F", "MES": "MES=F", "NQ": "NQ=F", "ES": "ES=F"}
ROUND_STEP = {"MNQ": 100, "NQ": 100, "MES": 50, "ES": 50}
SWINGS_PER_SIDE = 2          # nearest unbroken swing highs / lows kept per timeframe


@dataclass
class DayBars:
    symbol: str
    bars: list[dict]                                   # 5m RTH {"time": epoch (ET wall clock shown as UTC), o/h/l/c}
    levels: dict[str, float] = field(default_factory=dict)   # known before the open: name -> price
    note: str = ""
    bars1: list[dict] = field(default_factory=list)    # 1m, 08:00-16:00 ET
    vwap: list[dict] = field(default_factory=list)     # session VWAP from the open, per 1m bar {"time", "value"}
    htf: list[dict] = field(default_factory=list)      # 1H bars of the last few sessions, up to the open
    gap: str = ""                                      # opening gap, in words


class Bar(NamedTuple):
    t: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


def wall_epoch(dt: datetime, tz: str) -> int:
    """Chart library shows timestamps as UTC, so encode the ET wall-clock time as if it were UTC."""
    local = dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    return int(local.replace(tzinfo=timezone.utc).timestamp())


def _history(symbol: str, start: date, end: date, interval: str, zone: ZoneInfo) -> list[Bar]:
    import yfinance as yf

    hist = yf.Ticker(YAHOO.get(symbol, f"{symbol}=F")).history(
        start=start.isoformat(), end=end.isoformat(), interval=interval, auto_adjust=False)
    if hist.empty:
        return []
    hist = hist.tz_convert(zone)
    return [Bar(ts.to_pydatetime(), float(r.Open), float(r.High), float(r.Low), float(r.Close), float(r.Volume))
            for ts, r in hist.iterrows()]


def _as_dict(b: Bar, tz: str) -> dict:
    return {"time": wall_epoch(b.t, tz), "open": round(b.o, 2), "high": round(b.h, 2),
            "low": round(b.l, 2), "close": round(b.c, 2)}


def resample(bars: list[Bar], minutes: int, offset_min: int = 0) -> list[Bar]:
    """Group bars into bigger ones on the ET clock (4H uses offset 120 so buckets start at 18:00)."""
    out: dict[int, list[Bar]] = {}
    base = datetime(2000, 1, 1)
    for b in bars:
        sec = (b.t.replace(tzinfo=None) - base).total_seconds() - offset_min * 60
        out.setdefault(int(sec // (minutes * 60)), []).append(b)
    return [Bar(g[0].t, g[0].o, max(x.h for x in g), min(x.l for x in g), g[-1].c, sum(x.v for x in g))
            for _, g in sorted(out.items())]


def open_swings(bars: list, k: int = 2, hi=lambda b: b.h, lo=lambda b: b.l):
    """Swing highs / lows (higher / lower than k bars each side) that no later bar has traded through.
    Returns (highs nearest first, lows nearest first) as (bar, price)."""
    highs, lows = [], []
    for i in range(k, len(bars) - k):
        others = bars[i - k:i] + bars[i + 1:i + k + 1]
        rest = bars[i + 1:]
        if hi(bars[i]) > max(hi(x) for x in others) and all(hi(x) < hi(bars[i]) for x in rest):
            highs.append((bars[i], hi(bars[i])))
        if lo(bars[i]) < min(lo(x) for x in others) and all(lo(x) > lo(bars[i]) for x in rest):
            lows.append((bars[i], lo(bars[i])))
    return sorted(highs, key=lambda x: x[1]), sorted(lows, key=lambda x: -x[1])


def merge_levels(pairs: list[tuple[str, float]], tick: float = 0.25) -> dict[str, float]:
    """Same price from several sources becomes one line with a joined name."""
    out: list[list] = []
    for name, p in sorted(pairs, key=lambda x: x[1]):
        if out and abs(out[-1][1] - p) <= tick * 2:
            out[-1][0] += f" / {name}"
        else:
            out.append([name, round(p, 2)])
    return {n: p for n, p in out}


def _window(bars: list[Bar], start: datetime, end: datetime) -> list[Bar]:
    return [b for b in bars if start <= b.t < end]


def fetch_day(symbol: str, day: str, tz: str, start: str, end: str, lv: dict | None = None) -> DayBars:
    lv = lv or {}
    zone = ZoneInfo(tz)
    d = date.fromisoformat(day)
    s, e = time.fromisoformat(start), time.fromisoformat(end)
    open_dt = datetime.combine(d, s, zone)
    evening = datetime.combine(d - timedelta(days=1), time(18, 0), zone)

    r5 = _history(symbol, d - timedelta(days=7), d + timedelta(days=1), "5m", zone)
    if not r5:
        raise RuntimeError(f"拿不到 {symbol} 的 5 分钟行情（Yahoo 返回空数据）")
    rth = [b for b in r5 if b.t.date() == d and s <= b.t.time() < e]
    if not rth:
        raise RuntimeError(f"{day} 没有 {symbol} 的盘中行情（可能是休市日）")

    pairs: list[tuple[str, float]] = []
    gap = ""
    prev_days = sorted({b.t.date() for b in r5 if b.t.date() < d and s <= b.t.time() < e})
    if prev_days:
        prev = [b for b in r5 if b.t.date() == prev_days[-1] and s <= b.t.time() < e]
        pdc = prev[-1].c
        pairs += [("前日高 PDH", max(b.h for b in prev)), ("前日低 PDL", min(b.l for b in prev)), ("前日收 PDC", pdc)]
        diff = rth[0].o - pdc
        gap = (f"开盘 {rth[0].o:.2f}，比前日收盘 {pdc:.2f} {'高开' if diff > 0 else '低开'} {abs(diff):.2f} 点，"
               f"前日收盘 {pdc:.2f} 就是缺口回补位") if abs(diff) >= 0.5 else f"平开（开盘 {rth[0].o:.2f}，前日收盘 {pdc:.2f}）"

    asia_s, asia_e = (time.fromisoformat(x) for x in lv.get("asia", ["18:00", "03:00"]))
    lon_s, lon_e = (time.fromisoformat(x) for x in lv.get("london", ["03:00", "09:30"]))
    for name, a, b in (("隔夜", evening, open_dt),
                       ("亚盘", datetime.combine(evening.date(), asia_s, zone), datetime.combine(d, asia_e, zone)),
                       ("伦敦", datetime.combine(d, lon_s, zone), datetime.combine(d, lon_e, zone))):
        w = _window(r5, a, b)
        if w:
            code = {"隔夜": "ON", "亚盘": "Asia", "伦敦": "LDN"}[name]
            pairs += [(f"{name}高 {code}H", max(x.h for x in w)), (f"{name}低 {code}L", min(x.l for x in w))]

    # unbroken swing highs / lows on each timeframe, as of the open
    before_open5 = [b for b in r5 if b.t < open_dt]
    r60 = _history(symbol, d - timedelta(days=60), d + timedelta(days=1), "60m", zone)
    before_open60 = [b for b in r60 if b.t + timedelta(hours=1) <= open_dt]
    daily = _history(symbol, d - timedelta(days=400), d + timedelta(days=1), "1d", zone)
    before_today = [b for b in daily if b.t.date() < d]
    for name, bars in (("15m", resample(before_open5, 15)[-200:]),
                       ("1H", before_open60[-24 * 10:]),
                       ("4H", resample(before_open60, 240, 120)[-6 * 30:]),
                       ("日线", before_today[-120:])):
        highs, lows = open_swings(bars)
        pairs += [(f"{name}前高", p) for _, p in highs[:SWINGS_PER_SIDE]]
        pairs += [(f"{name}前低", p) for _, p in lows[:SWINGS_PER_SIDE]]

    monday = d - timedelta(days=d.weekday())
    last_week = [b for b in daily if monday - timedelta(days=7) <= b.t.date() < monday]
    if last_week:
        pairs += [("前周高 PWH", max(b.h for b in last_week)), ("前周低 PWL", min(b.l for b in last_week))]

    lo_day, hi_day = min(b.l for b in rth), max(b.h for b in rth)
    step = lv.get("round_step", {}).get(symbol, ROUND_STEP.get(symbol, 100))
    for k in range(int(lo_day // step), int(hi_day // step) + 2):
        pairs.append((f"整数 {k * step:g}", float(k * step)))

    r1 = _history(symbol, d, d + timedelta(days=1), "1m", zone)
    day1 = [b for b in r1 if b.t.date() == d and time(8, 0) <= b.t.time() < e]
    vwap, pv, vol = [], 0.0, 0.0
    for b in (b for b in day1 if b.t >= open_dt):
        pv += (b.h + b.l + b.c) / 3 * b.v
        vol += b.v
        if vol:
            vwap.append({"time": wall_epoch(b.t, tz), "value": round(pv / vol, 2)})

    return DayBars(symbol, [_as_dict(b, tz) for b in rth], merge_levels(pairs),
                   bars1=[_as_dict(b, tz) for b in day1], vwap=vwap,
                   htf=[_as_dict(b, tz) for b in before_open60[-24 * 5:]], gap=gap,
                   note="" if day1 else "拿不到 1 分钟行情（Yahoo 只保留最近约 30 天）")


def check_fills_in_range(day_bars: DayBars, prices: list[float]) -> str:
    """Yahoo uses the front-month contract; warn if fills look off (e.g. during contract roll)."""
    lo = min(b["low"] for b in day_bars.bars)
    hi = max(b["high"] for b in day_bars.bars)
    pad = (hi - lo) * 0.1
    off = [p for p in prices if p < lo - pad or p > hi + pad]
    return f"注意：{len(off)} 个成交价不在行情范围内，可能是换月合约价差。" if off else ""


def load_sample(root: Path) -> DayBars:
    data = json.loads((root / "samples/sample_bars.json").read_text(encoding="utf-8"))
    return DayBars(data["symbol"], data["bars"], data["levels"])
