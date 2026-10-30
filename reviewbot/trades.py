"""Turn raw fills into round-trip trades (flat -> position -> flat), per contract.

A "trade" here is everything from opening a position until it is flat again,
including scale-ins and partial exits, which is how a discretionary trader
usually counts "one trade".
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, time
from zoneinfo import ZoneInfo

MONTH_CODE = re.compile(r"^([A-Z]+?)([FGHJKMNQUVXZ])(\d{1,2})$")
CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"


def root_symbol(contract: str) -> str:
    """'MNQZ6' -> 'MNQ', 'MESZ26' -> 'MES'; anything else is returned unchanged."""
    c = contract.strip().upper().split(":")[-1].rstrip("!")
    m = MONTH_CODE.match(c)
    return m.group(1) if m else c


def circled(n: int) -> str:
    return CIRCLED[n - 1] if 1 <= n <= len(CIRCLED) else f"({n})"


@dataclass
class Fill:
    time: datetime          # timezone-aware
    contract: str           # e.g. MNQZ6
    side: str               # "Buy" or "Sell"
    qty: int
    price: float
    role: str = ""          # 进场 / 加仓 / 减仓 / 平仓 (filled in by pair_trades)
    order_id: int | None = None   # broker order that produced the fill (stop / limit / market)

    @property
    def signed_qty(self) -> int:
        return self.qty if self.side.lower().startswith("b") else -self.qty


@dataclass
class Trade:
    n: int
    contract: str
    symbol: str
    direction: str                      # "long" / "short"
    entry_time: datetime
    exit_time: datetime | None
    entry_avg: float
    exit_avg: float | None
    max_qty: int
    pnl_points_total: float             # points x contracts
    pnl_usd: float
    commission: float
    fills: list[Fill] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.exit_time is None

    @property
    def points_per_contract(self) -> float | None:
        if self.exit_avg is None:
            return None
        sign = 1 if self.direction == "long" else -1
        return round((self.exit_avg - self.entry_avg) * sign, 2)

    @property
    def label(self) -> str:
        return circled(self.n)


def _wavg(pairs: list[tuple[float, int]]) -> float:
    q = sum(x for _, x in pairs)
    return sum(p * x for p, x in pairs) / q if q else 0.0


def pair_trades(fills: list[Fill], point_value: dict, commission_per_side: float = 0.0) -> list[Trade]:
    """Group fills into round trips per contract, then number them by entry time."""
    by_contract: dict[str, list[Fill]] = {}
    for f in sorted(fills, key=lambda f: f.time):
        by_contract.setdefault(f.contract, []).append(f)

    trades: list[Trade] = []
    for contract, cfills in by_contract.items():
        sym = root_symbol(contract)
        pv = float(point_value.get(sym, 1))
        pos = 0
        cur: dict | None = None

        for f in cfills:
            remaining = f.signed_qty
            while remaining != 0:
                if pos == 0:
                    cur = {"dir": "long" if remaining > 0 else "short", "open": [], "close": [],
                           "fills": [], "entry_time": f.time, "max": 0}
                opening = (pos >= 0 and remaining > 0) or (pos <= 0 and remaining < 0)
                if opening:
                    step = remaining
                    role = "加仓" if cur["open"] else "进场"
                    cur["open"].append((f.price, abs(step)))
                else:
                    step = max(remaining, -pos) if remaining < 0 else min(remaining, -pos)
                    cur["close"].append((f.price, abs(step)))
                    role = "平仓" if pos + step == 0 else "减仓"
                pos += step
                remaining -= step
                cur["max"] = max(cur["max"], abs(pos))
                part = Fill(f.time, f.contract, f.side, abs(step), f.price, role)
                cur["fills"].append(part)
                if pos == 0:
                    trades.append(_finish(cur, contract, sym, pv, commission_per_side, f.time))
                    cur = None
        if cur is not None:  # still holding at the end of the data
            trades.append(_finish(cur, contract, sym, pv, commission_per_side, None))

    trades.sort(key=lambda t: t.entry_time)
    for i, t in enumerate(trades, 1):
        t.n = i
    return trades


def _finish(cur: dict, contract: str, sym: str, pv: float, comm: float, exit_time) -> Trade:
    sign = 1 if cur["dir"] == "long" else -1
    entry_avg = _wavg(cur["open"])
    exit_avg = _wavg(cur["close"]) if cur["close"] else None
    closed_qty = sum(q for _, q in cur["close"])
    pts_total = (exit_avg - entry_avg) * sign * closed_qty if exit_avg is not None else 0.0
    contracts_traded = sum(q for _, q in cur["open"]) + closed_qty
    commission = comm * contracts_traded
    return Trade(
        n=0, contract=contract, symbol=sym, direction=cur["dir"],
        entry_time=cur["entry_time"], exit_time=exit_time,
        entry_avg=round(entry_avg, 2), exit_avg=round(exit_avg, 2) if exit_avg is not None else None,
        max_qty=cur["max"], pnl_points_total=round(pts_total, 2),
        pnl_usd=round(pts_total * pv - commission, 2), commission=round(commission, 2),
        fills=cur["fills"],
    )


def filter_session(trades: list[Trade], tz: str, start: str, end: str) -> list[Trade]:
    """Keep trades opened inside the session window, then renumber."""
    zone = ZoneInfo(tz)
    s, e = time.fromisoformat(start), time.fromisoformat(end)
    kept = [t for t in trades if s <= t.entry_time.astimezone(zone).time() < e]
    for i, t in enumerate(kept, 1):
        t.n = i
    return kept


def day_stats(trades: list[Trade]) -> dict:
    closed = [t for t in trades if not t.is_open]
    wins = [t for t in closed if t.pnl_usd > 0]
    losses = [t for t in closed if t.pnl_usd < 0]
    total = round(sum(t.pnl_usd for t in closed), 2)
    return {
        "count": len(trades),
        "closed": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100) if closed else 0,
        "total_usd": total,
        "best_usd": max((t.pnl_usd for t in closed), default=0),
        "worst_usd": min((t.pnl_usd for t in closed), default=0),
        "symbols": sorted({t.symbol for t in trades}),
        "open_count": len(trades) - len(closed),
    }


def describe_stats(s: dict) -> str:
    return (
        f"品种：{'、'.join(s['symbols']) or '无'}；共 {s['count']} 笔，已平仓 {s['closed']} 笔，"
        f"盈 {s['wins']} / 亏 {s['losses']}，胜率 {s['win_rate']}%；"
        f"合计盈亏 ${s['total_usd']:+,.2f}；最大盈利 ${s['best_usd']:+,.2f}，最大亏损 ${s['worst_usd']:+,.2f}"
    )
