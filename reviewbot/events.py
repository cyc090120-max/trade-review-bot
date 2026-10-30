"""Each entry / add / scale-out / exit becomes one numbered "event" for the chart.

Events are numbered across the whole day in time order (①②③…), which is the
numbering used on the chart labels, in the list under the chart and by Gemini.
Trades (round trips) are referred to as 第1笔, 第2笔….
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

from .trades import Trade, circled


@dataclass
class Event:
    n: int
    trade_n: int
    symbol: str
    direction: str      # trade direction: long / short
    role: str           # 进场 / 加仓 / 减仓 / 平仓
    side: str           # Buy / Sell
    qty: int
    price: float
    time: datetime
    order_id: int | None = None

    @property
    def label(self) -> str:
        return circled(self.n)

    @property
    def is_buy(self) -> bool:
        return self.side.lower().startswith("b")

    @property
    def title(self) -> str:
        d = "多单" if self.direction == "long" else "空单"
        return f"{d}{self.role}"


def build_events(trades: list[Trade]) -> list[Event]:
    events: list[Event] = []
    for t in trades:
        merged: list[dict] = []
        for f in t.fills:
            key = (f.role, f.side, f.time.replace(second=0, microsecond=0))
            if merged and merged[-1]["key"] == key:      # split fills of one order
                m = merged[-1]
                m["px_qty"] += f.price * f.qty
                m["qty"] += f.qty
            else:
                merged.append({"key": key, "role": f.role, "side": f.side, "time": f.time,
                               "qty": f.qty, "px_qty": f.price * f.qty, "order_id": f.order_id})
        for m in merged:
            events.append(Event(0, t.n, t.symbol, t.direction, m["role"], m["side"], m["qty"],
                                round(m["px_qty"] / m["qty"], 2), m["time"], m["order_id"]))
    events.sort(key=lambda e: (e.time, e.trade_n))
    for i, e in enumerate(events, 1):
        e.n = i
    return events


def describe_day(trades: list[Trade], events: list[Event], tz: str) -> str:
    """Plain-text listing the model reads: trades with their numbered events nested."""
    zone = ZoneInfo(tz)
    if not trades:
        return "（今天没有交易）"
    lines = []
    for t in trades:
        side = "做多" if t.direction == "long" else "做空"
        if t.is_open:
            head = f"第{t.n}笔 {t.symbol} {side} 最大{t.max_qty}张（收盘时仍持仓）"
        else:
            held = int((t.exit_time - t.entry_time).total_seconds() // 60)
            head = (f"第{t.n}笔 {t.symbol} {side} 最大{t.max_qty}张 | 进场均价 {t.entry_avg} → 离场均价 {t.exit_avg}"
                    f" | 持仓 {held} 分钟 | 每张 {t.points_per_contract:+} 点 | 盈亏 ${t.pnl_usd:+,.2f}")
        lines.append(head)
        for e in (e for e in events if e.trade_n == t.n):
            act = "买" if e.is_buy else "卖"
            lines.append(f"   {e.label} {e.time.astimezone(zone):%H:%M} {e.role}：{act}{e.qty}张 @ {e.price}")
    return "\n".join(lines)
