"""Free 5-minute bars (Yahoo Finance via yfinance) plus the key levels drawn on the chart."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

YAHOO = {"MNQ": "MNQ=F", "MES": "MES=F", "NQ": "NQ=F", "ES": "ES=F"}


@dataclass
class DayBars:
    symbol: str
    bars: list[dict]                                   # {"time": epoch (ET wall clock shown as UTC), o/h/l/c}
    levels: dict[str, float] = field(default_factory=dict)
    note: str = ""


def wall_epoch(dt: datetime, tz: str) -> int:
    """Chart library shows timestamps as UTC, so encode the ET wall-clock time as if it were UTC."""
    local = dt.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    return int(local.replace(tzinfo=timezone.utc).timestamp())


def fetch_day(symbol: str, day: str, tz: str, start: str, end: str) -> DayBars:
    import yfinance as yf

    zone = ZoneInfo(tz)
    d = date.fromisoformat(day)
    hist = yf.Ticker(YAHOO.get(symbol, f"{symbol}=F")).history(
        start=(d - timedelta(days=7)).isoformat(), end=(d + timedelta(days=1)).isoformat(),
        interval="5m", auto_adjust=False,
    )
    if hist.empty:
        raise RuntimeError(f"拿不到 {symbol} 的 5 分钟行情（Yahoo 返回空数据）")
    hist = hist.tz_convert(zone)
    s, e = time.fromisoformat(start), time.fromisoformat(end)

    rows = [(ts.to_pydatetime(), r) for ts, r in hist.iterrows()]
    rth = [(t, r) for t, r in rows if t.date() == d and s <= t.time() < e]
    if not rth:
        raise RuntimeError(f"{day} 没有 {symbol} 的盘中行情（可能是休市日）")

    bars = [{"time": wall_epoch(t, tz), "open": round(float(r.Open), 2), "high": round(float(r.High), 2),
             "low": round(float(r.Low), 2), "close": round(float(r.Close), 2)} for t, r in rth]

    levels: dict[str, float] = {}
    prev_days = sorted({t.date() for t, _ in rows if t.date() < d and s <= t.time() < e})
    if prev_days:
        pd_ = prev_days[-1]
        prev = [r for t, r in rows if t.date() == pd_ and s <= t.time() < e]
        levels["前日高 PDH"] = round(float(max(r.High for r in prev)), 2)
        levels["前日低 PDL"] = round(float(min(r.Low for r in prev)), 2)
        on_start = datetime.combine(pd_, time(18, 0), zone)
        on_end = datetime.combine(d, s, zone)
        on = [r for t, r in rows if on_start <= t < on_end]
        if on:
            levels["隔夜高 ONH"] = round(float(max(r.High for r in on)), 2)
            levels["隔夜低 ONL"] = round(float(min(r.Low for r in on)), 2)
    return DayBars(symbol, bars, levels)


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
