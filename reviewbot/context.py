"""What the trader could see at the moment of each entry / exit, worked out from the data with no
hindsight, in ICT / SMC terms: where price sat between the levels, FVGs and order blocks, which
liquidity was just taken, whether a CISD had confirmed, how the 1-minute bars behaved. What happened
afterwards is listed separately and marked as reference only."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .events import Event
from . import smc
from .market import DayBars, merge_levels, open_swings, wall_epoch
from .orders import OrderBook, TradeRisk, exit_how
from .orders import describe as describe_risk
from .trades import Trade

SWEEP_LOOKBACK_MIN = 20
PRE_BARS = 12
CISD_BARS = 10
HINDSIGHT_MIN = 30
HI, LO = (lambda b: b["high"]), (lambda b: b["low"])



def _hm(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M")


def _ohlc(b: dict) -> str:
    return f"{_hm(b['time'])} {b['open']:.2f}/{b['high']:.2f}/{b['low']:.2f}/{b['close']:.2f}"


def _group(bars: list[dict], seconds: int) -> list[dict]:
    out: dict[int, list[dict]] = {}
    for b in bars:
        out.setdefault(b["time"] // seconds * seconds, []).append(b)
    return [{"time": t, "open": g[0]["open"], "high": max(HI(x) for x in g), "low": min(LO(x) for x in g),
             "close": g[-1]["close"]} for t, g in sorted(out.items())]


def visible_levels(db: DayBars, t: int) -> dict[str, float]:
    """Pre-open levels plus what the session itself had built by time t (completed bars only)."""
    pairs = list(db.levels.items())
    done5 = [b for b in db.bars if b["time"] + 300 <= t]
    if done5:
        pairs += [("今日盘中高", max(HI(b) for b in done5)), ("今日盘中低", min(LO(b) for b in done5))]
        for name, bars in (("5m前", done5), ("15m前", _group(done5, 900))):
            highs, lows = open_swings(bars, hi=HI, lo=LO)
            pairs += [(f"{name}高({_hm(b['time'])})", p) for b, p in highs[:2]]
            pairs += [(f"{name}低({_hm(b['time'])})", p) for b, p in lows[:2]]
    vw = [v for v in db.vwap if v["time"] + 60 <= t]
    if vw:
        pairs.append(("VWAP", vw[-1]["value"]))
    return merge_levels(pairs)


def visible_zones(db: DayBars, t: int, one_min_from: int | None = None) -> list[dict]:
    """Pre-open FVG / IFVG / OB / gaps still alive at time t, the session's own 5m ones, and (for an entry)
    1m ones formed since `one_min_from`. Only completed bars count."""
    done5 = [b for b in db.bars if b["time"] + 300 <= t]
    out = []
    for z in db.zones:
        alive = smc.track(z, [b for b in done5 if b["time"] >= z["start"]])
        if alive:
            out.append(alive)
    out += smc.zones(done5, "5m")
    if one_min_from is not None and db.bars1:
        done1 = [b for b in db.bars1 if one_min_from <= b["time"] and b["time"] + 60 <= t]
        out += smc.zones(done1, "1m")
    return out


def _zones_text(zones: list[dict], price: float, t: int) -> str:
    near = smc.nearest(zones, price)
    return "；".join(smc.describe_zone(z, price, t) for z in near) or "附近没有 FVG / OB / 缺口"


def _nearest(levels: dict[str, float], price: float, n: int = 3) -> str:
    above = sorted(((p, k) for k, p in levels.items() if p > price), key=lambda x: x[0])[:n]
    below = sorted(((p, k) for k, p in levels.items() if p <= price), key=lambda x: -x[0])[:n]
    fmt = lambda xs: "；".join(f"{k} {p:.2f}（{p - price:+.2f}）" for p, k in xs) or "无"
    return f"上方：{fmt(above)}\n    下方：{fmt(below)}"


def _sweeps(db: DayBars, bars: list[dict], t: int) -> list[str]:
    """Levels pierced and then closed back across within 3 bars, in the minutes before t."""
    start = t - SWEEP_LOOKBACK_MIN * 60
    levels = {k: v for k, v in visible_levels(db, start).items() if k != "VWAP"}  # VWAP moves, it isn't swept
    win = [b for b in bars if start <= b["time"] and b["time"] + 60 <= t]
    found = []
    for name, lv in levels.items():
        for i, b in enumerate(win):
            prev_close = win[i - 1]["close"] if i else b["open"]
            back = win[i:i + 4]
            if prev_close < lv < b["high"] and any(x["close"] < lv for x in back):
                found.append((b["time"], f"{_hm(b['time'])} 上刺 {name} {lv:.2f}（最高 {b['high']:.2f}）后收回下方"))
                break
            if prev_close > lv > b["low"] and any(x["close"] > lv for x in back):
                found.append((b["time"], f"{_hm(b['time'])} 下刺 {name} {lv:.2f}（最低 {b['low']:.2f}）后收回上方"))
                break
    return [s for _, s in sorted(found)][-4:]


def describe_levels(db: DayBars) -> str:
    first = db.bars[0]["open"]
    rows = [f"- {k} {p:.2f}（{p - first:+.2f}）" for k, p in sorted(db.levels.items(), key=lambda x: -x[1])]
    i = next((j for j, (k, p) in enumerate(sorted(db.levels.items(), key=lambda x: -x[1])) if p <= first), len(rows))
    rows.insert(i, f"  —— 开盘价 {first:.2f} ——")
    out = f"{db.symbol} 开盘前已知的关键位（从高到低，括号里是距开盘价的点数）：\n" + "\n".join(rows)
    return out + (f"\n开盘缺口：{db.gap}" if db.gap else "")


def describe_events(trades: list[Trade], events: list[Event], bars: dict[str, DayBars], tz: str,
                    risks: dict[int, TradeRisk] | None = None, book: OrderBook | None = None) -> str:
    blocks = []
    for ev in events:
        db = bars.get(ev.symbol)
        if not db:
            continue
        trade = next(t for t in trades if t.n == ev.trade_n)
        one_min = bool(db.bars1)
        bars_ = db.bars1 or db.bars
        step = 60 if one_min else 300
        t = wall_epoch(ev.time, tz)
        done = [b for b in bars_ if b["time"] + step <= t]
        act = "买" if ev.is_buy else "卖"
        lines = [f"{ev.label} {ev.time.astimezone(ZoneInfo(tz)):%H:%M:%S} {ev.title} {act}{ev.qty}张 @ {ev.price:.2f}"
                 f"（第{ev.trade_n}笔）",
                 "  【当时能看到的】",
                 f"  - 价格位置：{_nearest(visible_levels(db, t), ev.price)}"]
        sweeps = _sweeps(db, bars_, t) if one_min else []
        lines.append(f"  - 前 {SWEEP_LOOKBACK_MIN} 分钟的流动性扫荡：" + ("；".join(sweeps) if sweeps else "没有"))
        entry = ev.role in ("进场", "加仓")
        since = t - SWEEP_LOOKBACK_MIN * 60 if entry else None
        lines.append(f"  - FVG / OB / 缺口：{_zones_text(visible_zones(db, t, since), ev.price, t)}")
        if db.basis and db.index_name:
            bs = [x["value"] for x in db.basis if x["time"] + 60 <= t][-1:] or [db.basis[0]["value"]]
            cash = ev.price - bs[0]
            lines.append(f"  - 对应 {db.index_name} 约 {cash:.2f}（基差 {bs[0]:+.2f}）")
        if entry:
            win = [b for b in bars_ if b["time"] + step <= t][-CISD_BARS:]   # the move right before the entry
            c = smc.cisd(win, short=not ev.is_buy)
            if c:
                lines.append(f"  - CISD（{'1' if one_min else '5'} 分钟）：{c}")
            pre = done[-PRE_BARS:]
            lines.append(f"  - 出手前 {len(pre)} 根{'1' if one_min else '5'}分钟 K 线（开/高/低/收）：")
            lines += [f"    {_ohlc(b)}" for b in pre]
            risk = (risks or {}).get(trade.n)
            if ev.role == "进场" and risk:
                sign = 1 if trade.direction == "long" else -1
                plan = []
                if risk.initial_stop is not None:
                    plan.append(f"止损 {risk.initial_stop:.2f}（风险 {(ev.price - risk.initial_stop) * sign:.2f} 点/张）")
                if risk.initial_target is not None:
                    plan.append(f"止盈 {risk.initial_target:.2f}（目标 {(risk.initial_target - ev.price) * sign:.2f} 点）")
                lines.append("  - 进场时挂的单：" + ("，".join(plan) if plan else "进场后 30 秒内没有挂止损"))
        else:
            how = exit_how(book, ev.order_id)
            if how:
                lines.append(f"  - 出场方式：{how}")
            risk = (risks or {}).get(trade.n)
            if risk and ev.role == "平仓":
                lines.append(f"  - 这笔的止损记录：{describe_risk(trade, risk, tz)}")
            t0 = wall_epoch(trade.entry_time, tz)
            held = [b for b in done if b["time"] + step > t0]
            if len(held) > 40:  # long holds: show them as 5-minute bars
                held, step_txt = _group(held, 300), "5"
            else:
                step_txt = "1" if one_min else "5"
            sign = 1 if trade.direction == "long" else -1
            if held:
                mfe = max((HI(b) if sign > 0 else -LO(b)) for b in held) - sign * trade.entry_avg
                mae = min((LO(b) if sign > 0 else -HI(b)) for b in held) - sign * trade.entry_avg
                lines.append(f"  - 持仓期间（已走完的 K 线）每张最大浮盈 {mfe:+.2f} 点，最大浮亏 {mae:+.2f} 点")
            lines.append(f"  - 进场后到出手前的{step_txt}分钟 K 线（开/高/低/收）：")
            lines += [f"    {_ohlc(b)}" for b in held] or ["    （进场后不到一根 K 线就出手）"]
        after = [b for b in bars_ if t < b["time"] + step and b["time"] <= t + HINDSIGHT_MIN * 60]
        if after:
            hi, lo = max(HI(b) for b in after), min(LO(b) for b in after)
            lines.append(f"  【事后，仅供参考，不能作为评价依据】之后 {HINDSIGHT_MIN} 分钟最高 {hi:.2f}"
                         f"（{hi - ev.price:+.2f}），最低 {lo:.2f}（{lo - ev.price:+.2f}）")
        blocks.append("\n".join(lines))
    note = "" if all(db.bars1 for db in bars.values()) else "（部分品种没有 1 分钟行情，改用 5 分钟）\n\n"
    return note + "\n\n".join(blocks)
