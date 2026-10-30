"""Free market data (Yahoo Finance via yfinance): the day's 5- and 1-minute bars, the 1-hour bars
before the open, and what an ICT / SMC trader marks before the open: previous day / week high-low,
overnight / Asia / London high-low, unbroken swing highs and lows (and equal highs / lows) on
15m / 1H / 4H / daily, unfilled fair value gaps, NDOG / NWOG and the opening gap, round numbers
(for MES: every 10 points of the SPX cash index, moved by the futures basis)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo

from . import smc

YAHOO = {"MNQ": "MNQ=F", "MES": "MES=F", "NQ": "NQ=F", "ES": "ES=F"}
ROUND = {"MNQ": {"step": 100}, "NQ": {"step": 100},
         "MES": {"index": "^GSPC", "step": 10}, "ES": {"index": "^GSPC", "step": 10}}
SWINGS_PER_SIDE = 2          # nearest unbroken swing highs / lows / FVGs kept per timeframe
MIN_GAP = 0.5                # points; smaller opening gaps are ignored


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
    zones: list[dict] = field(default_factory=list)    # FVG / IFVG / OB / gaps known before the open (see smc.py)
    basis: list[dict] = field(default_factory=list)    # futures minus cash index, per 1m bar {"time", "value"}
    index_name: str = ""                               # e.g. SPX, when round numbers follow the cash index


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

    ticker = symbol if symbol.startswith("^") else YAHOO.get(symbol, f"{symbol}=F")  # ^GSPC is the cash index
    hist = yf.Ticker(ticker).history(
        start=start.isoformat(), end=end.isoformat(), interval=interval, auto_adjust=False)
    if hist.empty:
        return []
    hist = hist.tz_convert(zone)
    return clean([Bar(ts.to_pydatetime(), float(r.Open), float(r.High), float(r.Low), float(r.Close), float(r.Volume))
                  for ts, r in hist.iterrows()])


BAD_BAR_FACTOR = 8   # an open / high / low this many median bar ranges away from where price really was is a bad print


def clean(bars: list[Bar]) -> list[Bar]:
    """Yahoo sometimes resumes after a data gap with a stale open (e.g. the previous day's close), which
    draws one huge fake candle and squashes the whole chart. Pull such opens back to the previous close
    and drop highs / lows that only the bad open produced."""
    if len(bars) < 3:
        return bars
    ranges = sorted(b.h - b.l for b in bars)
    limit = max(ranges[len(ranges) // 2], 0.25) * BAD_BAR_FACTOR
    out = [bars[0]]
    for b in bars[1:]:
        prev = out[-1].c
        o, h, l = b.o, b.h, b.l
        if abs(o - prev) > limit and abs(b.c - prev) <= limit:
            o = prev
        if h - max(o, b.c) > limit:
            h = max(o, b.c)
        if min(o, b.c) - l > limit:
            l = min(o, b.c)
        out.append(Bar(b.t, o, max(h, o, b.c), min(l, o, b.c), b.c, b.v))   # keep the candle consistent
    return out


def _as_dict(b: Bar, tz: str) -> dict:
    return {"time": wall_epoch(b.t, tz), "open": round(b.o, 2), "high": round(b.h, 2),
            "low": round(b.l, 2), "close": round(b.c, 2), "volume": int(b.v)}


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


def equal_levels(swings: list, tol: float = 0.5) -> list[float]:
    """Two or more swing points within tol of each other: resting liquidity (EQH / EQL)."""
    prices = sorted(p for _, p in swings)
    return [round((a + b) / 2, 2) for a, b in zip(prices, prices[1:]) if b - a <= tol]


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

    asia_s, asia_e = (time.fromisoformat(x) for x in lv.get("asia", ["20:00", "00:00"]))
    lon_s, lon_e = (time.fromisoformat(x) for x in lv.get("london", ["02:00", "05:00"]))
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
    zones: list[dict] = []
    first_open = rth[0].o
    for name, bars in (("15m", resample(before_open5, 15)[-200:]),
                       ("1H", before_open60[-24 * 10:]),
                       ("4H", resample(before_open60, 240, 120)[-6 * 30:]),
                       ("日线", before_today[-120:])):
        highs, lows = open_swings(bars)
        pairs += [(f"{name}前高", p) for _, p in highs[:SWINGS_PER_SIDE]]
        pairs += [(f"{name}前低", p) for _, p in lows[:SWINGS_PER_SIDE]]
        if name in ("15m", "1H"):
            pairs += [(f"{name}等高 EQH", p) for p in equal_levels(highs)]
            pairs += [(f"{name}等低 EQL", p) for p in equal_levels(lows)]
        if name != "日线":
            found = smc.zones([_as_dict(b, tz) for b in bars], name)
            zones += smc.nearest([z for z in found if z["type"] != "OB"], first_open)
            zones += smc.nearest([z for z in found if z["type"] == "OB"], first_open, n=1)

    # ICT opening gaps: NDOG (17:00 close -> 18:00 reopen), NWOG (Friday close -> Sunday reopen)
    sunday = datetime.combine(d - timedelta(days=d.weekday() + 1), time(18, 0), zone)
    for name, reopen in (("NDOG 日开盘缺口", evening), ("NWOG 周开盘缺口", sunday)):
        before = [b for b in r5 if reopen - timedelta(days=3) <= b.t < reopen - timedelta(minutes=30)]
        after = [b for b in r5 if reopen <= b.t < reopen + timedelta(hours=1)]
        if before and after and abs(after[0].o - before[-1].c) >= MIN_GAP and not (name.startswith("NDOG") and reopen == sunday):
            z_lo, z_hi = sorted((before[-1].c, after[0].o))
            zones.append({"name": name, "tf": "", "type": "GAP", "kind": "gap", "lo": round(z_lo, 2),
                          "hi": round(z_hi, 2), "start": wall_epoch(after[0].t, tz), "state": ""})
    if prev_days and abs(rth[0].o - pdc) >= MIN_GAP:
        z_lo, z_hi = sorted((pdc, rth[0].o))
        zones.append({"name": "开盘缺口 RTH", "tf": "", "type": "GAP", "kind": "gap", "lo": round(z_lo, 2),
                      "hi": round(z_hi, 2), "start": wall_epoch(rth[0].t, tz), "state": ""})

    monday = d - timedelta(days=d.weekday())
    last_week = [b for b in daily if monday - timedelta(days=7) <= b.t.date() < monday]
    if last_week:
        pairs += [("前周高 PWH", max(b.h for b in last_week)), ("前周低 PWL", min(b.l for b in last_week))]

    r1 = _history(symbol, d, d + timedelta(days=1), "1m", zone)
    day1 = [b for b in r1 if b.t.date() == d and time(8, 0) <= b.t.time() < e]

    # round numbers, on the futures price or (MES) on the SPX cash index moved by the basis
    rn = {**ROUND.get(symbol, {"step": 100}), **lv.get("round_numbers", {}).get(symbol, {})}
    step, basis, index_name = rn["step"], [], ""
    if rn.get("index"):
        cash = {b.t: b.c for b in _history(rn["index"], d, d + timedelta(days=1), "1m", zone)}
        basis = [{"time": wall_epoch(b.t, tz), "value": round(b.c - cash[b.t], 2)} for b in day1 if b.t in cash]
        index_name = {"^GSPC": "SPX", "^NDX": "NDX"}.get(rn["index"], rn["index"])
    lo_day, hi_day = min(b.l for b in rth), max(b.h for b in rth)
    if basis:
        opening = sorted(x["value"] for x in basis[:15])
        shift = opening[len(opening) // 2]           # basis around the open; it drifts a point or two a day
        for k in range(int((lo_day - shift) // step), int((hi_day - shift) // step) + 2):
            pairs.append((f"{index_name} {k * step:g}", k * step + shift))
    else:
        for k in range(int(lo_day // step), int(hi_day // step) + 2):
            pairs.append((f"整数 {k * step:g}", float(k * step)))
    vwap, pv, vol = [], 0.0, 0.0
    for b in (b for b in day1 if b.t >= open_dt):
        pv += (b.h + b.l + b.c) / 3 * b.v
        vol += b.v
        if vol:
            vwap.append({"time": wall_epoch(b.t, tz), "value": round(pv / vol, 2)})

    return DayBars(symbol, [_as_dict(b, tz) for b in rth], merge_levels(pairs),
                   bars1=[_as_dict(b, tz) for b in day1], vwap=vwap,
                   htf=[_as_dict(b, tz) for b in before_open60[-24 * 5:]], gap=gap, zones=zones,
                   basis=basis, index_name=index_name,
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
