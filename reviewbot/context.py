"""What the trader could see at the moment of each entry / exit, worked out from the data with no
hindsight, in ICT / SMC terms: where price sat between the levels and fair value gaps, premium or
discount, which liquidity was just swept, whether the 1-minute chart shifted structure with
displacement, which session window it was. What happened afterwards is listed separately and
marked as reference only."""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .events import Event
from .market import DayBars, merge_levels, nearest_zones, open_fvgs, open_swings, wall_epoch
from .trades import Trade

SWEEP_LOOKBACK_MIN = 20
PRE_BARS = 12
HINDSIGHT_MIN = 30
HI, LO = (lambda b: b["high"]), (lambda b: b["low"])
# ICT session windows during the US cash session (ET)
WINDOWS = [("09:30", "10:00", "NY AM 开盘半小时"), ("10:00", "11:00", "NY AM 银弹窗口 10–11 点"),
           ("11:00", "12:00", "NY AM 尾段"), ("12:00", "13:30", "午休时段（NY Lunch）"),
           ("13:30", "14:00", "NY PM 开始"), ("14:00", "15:00", "NY PM 银弹窗口 14–15 点"),
           ("15:00", "16:00", "收盘前一小时")]


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


def visible_zones(db: DayBars, t: int) -> list[dict]:
    """Pre-open FVGs / gaps not yet traded through by time t, plus 5m FVGs the session has made."""
    done5 = [b for b in db.bars if b["time"] + 300 <= t]
    out = []
    for z in db.zones:
        later = [b for b in done5 if b["time"] >= z["start"]]
        if z["kind"] == "bull" and any(LO(b) <= z["lo"] for b in later):
            continue
        if z["kind"] == "bear" and any(HI(b) >= z["hi"] for b in later):
            continue
        out.append(z)
    for kind, lo, hi, b in open_fvgs(done5, hi=HI, lo=LO):
        out.append({"name": f"5m {'看涨' if kind == 'bull' else '看跌'} FVG({_hm(b['time'])})", "kind": kind,
                    "lo": lo, "hi": hi, "start": b["time"]})
    return out


def _zones_text(zones: list[dict], price: float) -> str:
    near = nearest_zones([(z["name"], z["lo"], z["hi"]) for z in zones], price)
    if not near:
        return "附近没有未回补的 FVG / 缺口"
    def where(lo, hi):
        return "价格在区内" if lo <= price <= hi else (f"在上方 {lo - price:+.2f}" if lo > price else f"在下方 {hi - price:+.2f}")
    return "；".join(f"{n} {lo:.2f}–{hi:.2f}（{where(lo, hi)}）" for n, lo, hi in near)


def _premium_discount(db: DayBars, bars: list[dict], t: int, price: float) -> str:
    rth = [b for b in bars if b["time"] >= db.bars[0]["time"] and b["time"] + 60 <= t]
    if len(rth) < 15:  # too early in the session: use the overnight range
        on = {k: p for k, p in db.levels.items() if "隔夜" in k}
        hi = max([p for k, p in on.items() if "高" in k] + [HI(b) for b in rth], default=None)
        lo = min([p for k, p in on.items() if "低" in k] + [LO(b) for b in rth], default=None)
        src = "隔夜 + 开盘区间"
    else:
        hi, lo, src = max(HI(b) for b in rth), min(LO(b) for b in rth), "今日盘中区间"
    if hi is None or lo is None or hi <= lo:
        return ""
    pct = (price - lo) / (hi - lo) * 100
    zone = "溢价区" if pct > 50 else "折价区"
    return f"{src} {lo:.2f}–{hi:.2f}，价格在 {pct:.0f}% 位置（{zone}，50% 平衡位 {(hi + lo) / 2:.2f}）"


def _mss(bars: list[dict], t: int, short: bool) -> str:
    """After the extreme of the last 20 minutes, did a 1m candle close through the last swing point
    that formed before it (market structure shift), and with a big body (displacement)?"""
    win = [b for b in bars if t - SWEEP_LOOKBACK_MIN * 60 <= b["time"] and b["time"] + 60 <= t]
    if len(win) < 5:
        return ""
    ext = max(range(len(win)), key=lambda i: HI(win[i])) if short else min(range(len(win)), key=lambda i: LO(win[i]))
    pivots = [i for i in range(1, ext) if (LO(win[i]) < LO(win[i - 1]) and LO(win[i]) <= LO(win[i + 1]) if short
                                           else HI(win[i]) > HI(win[i - 1]) and HI(win[i]) >= HI(win[i + 1]))]
    word = "高点" if short else "低点"
    if not pivots:
        return f"前 20 分钟的{word} {_hm(win[ext]['time'])} 之前没有可参照的 1 分钟结构点，谈不上 MSS"
    level = LO(win[pivots[-1]]) if short else HI(win[pivots[-1]])
    avg_body = sum(abs(b["close"] - b["open"]) for b in win) / len(win)
    for b in win[ext + 1:]:
        if (b["close"] < level) if short else (b["close"] > level):
            disp = "，带位移（大实体）" if abs(b["close"] - b["open"]) >= 1.5 * avg_body else "，实体不大"
            return (f"{_hm(win[ext]['time'])} 做出{word}后，{_hm(b['time'])} 1 分钟收{'破' if short else '上'}"
                    f" {_hm(win[pivots[-1]]['time'])} 的结构点 {level:.2f}，出现 MSS{disp}")
    return (f"{_hm(win[ext]['time'])} 做出{word}后，1 分钟还没有收{'破' if short else '上'}"
            f"结构点 {level:.2f}（{_hm(win[pivots[-1]]['time'])}），没有 MSS")


def _window_name(t: int) -> str:
    hm = _hm(t)
    return next((name for a, b, name in WINDOWS if a <= hm < b), "")


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


def describe_events(trades: list[Trade], events: list[Event], bars: dict[str, DayBars], tz: str) -> str:
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
        lines.append(f"  - FVG / 缺口：{_zones_text(visible_zones(db, t), ev.price)}")
        pd_ = _premium_discount(db, bars_, t, ev.price)
        if pd_:
            lines.append(f"  - 溢价 / 折价：{pd_}")
        if db.basis and db.index_name:
            bs = [x["value"] for x in db.basis if x["time"] + 60 <= t][-1:] or [db.basis[0]["value"]]
            cash = ev.price - bs[0]
            lines.append(f"  - 对应 {db.index_name} 约 {cash:.2f}（基差 {bs[0]:+.2f}）")
        lines.append(f"  - 时段：{_window_name(t) or '盘中'}")
        if ev.role in ("进场", "加仓"):
            short = (ev.side.lower().startswith("s"))
            mss = _mss(bars_, t, short) if one_min else ""
            if mss:
                lines.append(f"  - 1 分钟结构：{mss}")
            pre = done[-PRE_BARS:]
            lines.append(f"  - 出手前 {len(pre)} 根{'1' if one_min else '5'}分钟 K 线（开/高/低/收）：")
            lines += [f"    {_ohlc(b)}" for b in pre]
        else:
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
