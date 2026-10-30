"""ICT / SMC concepts, computed the same way every day. ICT never published an official glossary; these
follow the most cited public write-up (LuxAlgo's concept library) and stick to one convention each:

- FVG: three candles. Bullish when candle 3's low is above candle 1's high; the zone runs from candle 1's
  high to candle 3's low (bearish mirrored). CE is the midpoint. Once a candle body closes beyond the far
  edge the gap has failed and flips role: it becomes an IFVG.
- Order block: the last opposite-closing candle before a displacement leg (a run of them is allowed by some
  write-ups, but it makes blocks too tall to use). Displacement here = the leg leaves an FVG and takes out
  the extreme of the bars before the block. The zone is the full range including wicks. A body close
  beyond the far edge invalidates it.
- CISD: take the extreme of the recent move and the last unbroken run of candles delivering price into
  it; the level is the open of the first candle in that run. Confirmed when a candle body closes beyond
  that open (wicks don't count).

Bars are dicts {"time", "open", "high", "low", "close"}.
"""
from __future__ import annotations

from datetime import datetime, timezone

MIN_SIZE_FACTOR = 0.15       # ignore gaps / blocks smaller than this share of the median bar range
OB_LOOKBACK = 10             # the displacement must take out the extreme of this many bars before the block


def _hm(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%H:%M")


def _up(b: dict) -> bool:
    return b["close"] > b["open"]


def _down(b: dict) -> bool:
    return b["close"] < b["open"]


def min_size(bars: list[dict]) -> float:
    ranges = sorted(b["high"] - b["low"] for b in bars) or [0]
    return max(0.5, ranges[len(ranges) // 2] * MIN_SIZE_FACTOR)


def _raw_fvgs(bars: list[dict], size: float) -> list[tuple[int, str, float, float]]:
    out = []
    for i in range(1, len(bars) - 1):
        a, c = bars[i - 1], bars[i + 1]
        if a["high"] < c["low"] and c["low"] - a["high"] >= size:
            out.append((i, "bull", a["high"], c["low"]))
        elif a["low"] > c["high"] and a["low"] - c["high"] >= size:
            out.append((i, "bear", c["high"], a["low"]))
    return out


def track(z: dict, later: list[dict]) -> dict | None:
    """Walk the bars after the zone formed: mitigation, FVG -> IFVG, invalidation. None = gone."""
    z = dict(z)
    for b in later:
        bull = z["kind"] == "bull"
        through = b["close"] < z["lo"] if bull else b["close"] > z["hi"]      # body close beyond far edge
        if through:
            if z["type"] == "FVG":       # failed gap flips role
                z.update(type="IFVG", kind="bear" if bull else "bull", state="已反转为 IFVG")
                continue
            return None                  # OB / IFVG closed through: done
        if z["type"] == "GAP":
            continue
        touched = b["low"] <= z["hi"] if bull else b["high"] >= z["lo"]
        ce = (z["lo"] + z["hi"]) / 2
        if touched and z["state"] in ("未回踩", "已反转为 IFVG"):
            z["state"] = "已回踩"
        if (b["low"] <= ce if bull else b["high"] >= ce) and z["type"] == "FVG":
            z["state"] = "已回补到 CE"
    return z


def label(z: dict) -> str:
    word = {"bull": "看涨", "bear": "看跌"}.get(z["kind"], "")
    return f"{z['tf']} {word} {z['type']}" if z["type"] != "GAP" else z["name"]


def zones(bars: list[dict], tf: str, upto: list[dict] | None = None) -> list[dict]:
    """Active FVGs / IFVGs / order blocks formed in `bars`, tracked through `bars` (+ `upto`, later bars)."""
    size = min_size(bars)
    later_all = bars + (upto or [])
    out, seen_ob = [], set()
    for i, kind, lo, hi in _raw_fvgs(bars, size):
        z = {"tf": tf, "type": "FVG", "kind": kind, "lo": round(lo, 2), "hi": round(hi, 2),
             "start": bars[i - 1]["time"], "state": "未回踩"}
        t = track(z, later_all[i + 2:])
        if t:
            out.append(t)
        # order block: last opposite-closing candle(s) before this displacement leg
        opposite = _down if kind == "bull" else _up
        j = i - 1
        while j >= 0 and not opposite(bars[j]):
            j -= 1
        if j < 0 or (j, kind) in seen_ob:
            continue
        s = j
        prior = bars[max(0, s - OB_LOOKBACK):s]
        leg = bars[j + 1:i + 2]
        if not prior or not (max(b["high"] for b in leg) > max(b["high"] for b in prior) if kind == "bull"
                             else min(b["low"] for b in leg) < min(b["low"] for b in prior)):
            continue
        seen_ob.add((j, kind))
        block = bars[s:j + 1]
        ob = {"tf": tf, "type": "OB", "kind": kind, "lo": round(min(b["low"] for b in block), 2),
              "hi": round(max(b["high"] for b in block), 2), "start": bars[s]["time"], "state": "未回踩"}
        if ob["hi"] - ob["lo"] >= size:
            t = track(ob, later_all[i + 2:])
            if t:
                out.append(t)
    for z in out:
        z["name"] = label(z)
    return out


def nearest(zs: list[dict], price: float, n: int = 2) -> list[dict]:
    """Zones price is sitting in, plus the n nearest above and below."""
    inside = [z for z in zs if z["lo"] <= price <= z["hi"]]
    above = sorted((z for z in zs if z["lo"] > price), key=lambda z: z["lo"])[:n]
    below = sorted((z for z in zs if z["hi"] < price), key=lambda z: -z["hi"])[:n]
    return inside + above + below


def describe_zone(z: dict, price: float, now: int) -> str:
    if z["lo"] <= price <= z["hi"]:
        where = "价格在区内"
    elif z["lo"] > price:
        where = f"在上方 {z['lo'] - price:+.2f}"
    else:
        where = f"在下方 {z['hi'] - price:+.2f}"
    extra = f"，CE {(z['lo'] + z['hi']) / 2:.2f}" if z["type"] in ("FVG", "IFVG") else ""
    state = f"，{z['state']}" if z.get("state") and z["type"] != "GAP" else ""
    fmt = "%H:%M" if z["start"] // 86400 == now // 86400 else "%m-%d %H:%M"
    when = datetime.fromtimestamp(z["start"], timezone.utc).strftime(fmt)
    return f"{z['name']}（{when} 形成）{z['lo']:.2f}–{z['hi']:.2f}（{where}{extra}{state}）"


def cisd(win: list[dict], short: bool) -> str:
    """CISD on completed bars before an entry. short=True looks at the recent high."""
    if len(win) < 4:
        return ""
    ext = max(range(len(win)), key=lambda i: win[i]["high"]) if short else min(range(len(win)), key=lambda i: win[i]["low"])
    deliver = _up if short else _down
    end = ext
    while end >= 0 and not deliver(win[end]):
        end -= 1
    if end < 0:
        return f"{_hm(win[ext]['time'])} 的{'高' if short else '低'}点之前找不到连续{'阳' if short else '阴'}线，无法定 CISD"
    start = end
    while start - 1 >= 0 and deliver(win[start - 1]):
        start -= 1
    level = win[start]["open"]
    head = (f"{_hm(win[ext]['time'])} 做出{'高' if short else '低'}点 {win[ext]['high' if short else 'low']:.2f}，"
            f"推上去的那段连续{'阳' if short else '阴'}线从 {_hm(win[start]['time'])} 开始，CISD 位 = 开盘价 {level:.2f}")
    for b in win[max(ext, end) + 1:]:
        if (b["close"] < level) if short else (b["close"] > level):
            return f"{head}；{_hm(b['time'])} 实体收{'破' if short else '上'}该位，CISD 已确认"
    return f"{head}；出手前还没有 K 线实体收{'破' if short else '上'}该位，CISD 未确认"
