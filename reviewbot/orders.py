"""Stops and targets from Tradovate's order records: which protective stop and profit target were working
during each trade, where they started, every time they were moved, and how the trade ended.

Raw records (Tradovate REST entities): orders (id, contractId, action, ordStatus), orderVersions (one per
New / Modify command: orderType, price, stopPrice; the version id is the command id), commands
(timestamp, commandType), executionReports (execType New / Replaced / Canceled / Trade).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .trades import Trade

STOP_TYPES = ("Stop", "StopLimit", "TrailingStop", "TrailingStopLimit")


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


@dataclass
class Leg:
    """One stop or target order: its price over time and how it ended."""
    kind: str                                          # "stop" / "target"
    order_id: int
    steps: list[tuple[datetime, float]]                # (time it was set, price)
    end: datetime | None = None
    end_how: str = ""                                  # 成交 / 撤单

    def price_at(self, t: datetime) -> float | None:
        p = None
        for when, price in self.steps:
            if when <= t:
                p = price
        return p


@dataclass
class TradeRisk:
    stops: list[Leg] = field(default_factory=list)
    targets: list[Leg] = field(default_factory=list)
    initial_stop: float | None = None
    initial_target: float | None = None


class OrderBook:
    def __init__(self, raw: dict):
        cmd_time = {c["id"]: _ts(c["timestamp"]) for c in raw.get("commands", [])}
        self.contract_ids = {c["name"]: c["id"] for c in raw.get("contracts", [])}
        self.orders = {o["id"]: o for o in raw.get("orders", [])}
        self.versions: dict[int, list[tuple[datetime, dict]]] = {}
        for v in raw.get("versions", []):
            when = cmd_time.get(v["id"])
            if when:
                self.versions.setdefault(v["orderId"], []).append((when, v))
        for vs in self.versions.values():
            vs.sort(key=lambda x: x[0])
        self.ended: dict[int, tuple[datetime, str]] = {}
        for r in sorted(raw.get("reports", []), key=lambda r: r["timestamp"]):
            if r.get("execType") in ("Canceled", "Expired", "Rejected"):
                self.ended[r["orderId"]] = (_ts(r["timestamp"]), "撤单")
            elif r.get("execType") == "Trade" and r.get("ordStatus") == "Filled":
                self.ended[r["orderId"]] = (_ts(r["timestamp"]), "成交")

    def order_type(self, order_id: int | None) -> str:
        vs = self.versions.get(order_id or -1)
        return vs[0][1]["orderType"] if vs else ""

    def risk(self, trade: Trade) -> TradeRisk:
        """Stop / target orders on the exit side of this trade's contract that were working while it was open."""
        cid = self.contract_ids.get(trade.contract)
        exit_side = "Buy" if trade.direction == "short" else "Sell"
        t_in = trade.entry_time - timedelta(seconds=2)
        t_out = (trade.exit_time or trade.entry_time + timedelta(hours=8)) + timedelta(seconds=2)
        out = TradeRisk()
        for oid, o in self.orders.items():
            vs = self.versions.get(oid)
            if not vs or o.get("contractId") != cid or o.get("action") != exit_side:
                continue
            kind = vs[0][1]["orderType"]
            start = vs[0][0]
            end, how = self.ended.get(oid, (None, ""))
            if start > t_out or (end and end < t_in):
                continue
            if kind in STOP_TYPES:
                steps = [(w, float(v["stopPrice"])) for w, v in vs if v.get("stopPrice") is not None]
                if steps:
                    out.stops.append(Leg("stop", oid, steps, end, how))
            elif kind == "Limit":
                steps = [(w, float(v["price"])) for w, v in vs if v.get("price") is not None]
                profit_side = (steps and (steps[0][1] < trade.entry_avg if trade.direction == "short"
                                          else steps[0][1] > trade.entry_avg))
                if profit_side:
                    out.targets.append(Leg("target", oid, steps, end, how))
        # what was protecting the position right after the entry (allow a few seconds to place the bracket)
        settle = trade.entry_time + timedelta(seconds=30)
        firsts = [l.price_at(settle) or l.steps[0][1] for l in out.stops if l.steps[0][0] <= settle + timedelta(seconds=30)]
        if firsts:
            out.initial_stop = min(firsts, key=lambda p: abs(p - trade.entry_avg))
        tfirsts = [l.price_at(settle) or l.steps[0][1] for l in out.targets if l.steps[0][0] <= settle + timedelta(seconds=30)]
        if tfirsts:
            out.initial_target = min(tfirsts, key=lambda p: abs(p - trade.entry_avg))
        return out


def exit_how(book: OrderBook | None, order_id: int | None) -> str:
    kind = book.order_type(order_id) if book else ""
    return {"Stop": "止损单成交", "StopLimit": "止损单成交", "TrailingStop": "移动止损成交",
            "Limit": "限价单成交", "Market": "市价单"}.get(kind, "")


def describe(trade: Trade, risk: TradeRisk, tz: str) -> str:
    """Plain text for the model: the stop and target plan of one trade and what happened to it."""
    zone = ZoneInfo(tz)
    hm = lambda t: t.astimezone(zone).strftime("%H:%M:%S")
    sign = 1 if trade.direction == "long" else -1
    lines = []
    if risk.initial_stop is not None:
        r = (trade.entry_avg - risk.initial_stop) * sign
        line = f"进场时止损 {risk.initial_stop:.2f}（风险 {r:.2f} 点/张）"
        if risk.initial_target is not None:
            rew = (risk.initial_target - trade.entry_avg) * sign
            line += f"，止盈 {risk.initial_target:.2f}（目标 {rew:.2f} 点，盈亏比 1:{rew / r:.1f}）" if r > 0 else ""
        if trade.points_per_contract is not None and r > 0:
            line += f"；这笔结果 {trade.points_per_contract / r:+.1f}R"
        lines.append(line)
    else:
        lines.append("进场后 30 秒内没有挂止损单" + ("" if risk.stops else "，整笔持仓都没有止损单"))
    settle = trade.entry_time + timedelta(seconds=30)   # changes this soon after entry are the initial placement
    events: list[tuple[datetime, str]] = []
    for leg in risk.stops:
        prev = leg.price_at(settle) or leg.steps[0][1]
        for w, p in leg.steps:
            if settle < w and (not trade.exit_time or w <= trade.exit_time) and p != prev:
                events.append((w, f"{hm(w)} 止损从 {prev:.2f} 移到 {p:.2f}（{'收紧 / 推保护' if (p - prev) * sign > 0 else '放宽'}）"))
            if w > settle:
                prev = p if (not trade.exit_time or w <= trade.exit_time) else prev
        if leg.end and leg.end_how and (not trade.exit_time or leg.end <= trade.exit_time + timedelta(seconds=2)):
            events.append((leg.end, f"{hm(leg.end)} 止损单{'被打成交' if leg.end_how == '成交' else '撤掉'}（最后价格 {prev:.2f}）"))
    lines += [text for _, text in sorted(events)]
    if risk.initial_stop is None and risk.stops:
        first = min(risk.stops, key=lambda l: l.steps[0][0])
        lines.append(f"{hm(first.steps[0][0])} 才挂上止损 {first.steps[0][1]:.2f}")
    return "；".join(lines)
