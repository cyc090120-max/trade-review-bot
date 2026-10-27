"""Demo data so the whole pipeline can run before any account is connected.

Fill prices are taken from the sample bars so every arrow lands on a real candle.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .trades import Fill

ET = ZoneInfo("America/New_York")
SAMPLE_DATE = "2026-10-27"
ROOT = Path(__file__).resolve().parent.parent


def _bars() -> dict[str, dict]:
    data = json.loads((ROOT / "samples/sample_bars.json").read_text(encoding="utf-8"))
    return {datetime.fromtimestamp(b["time"], timezone.utc).strftime("%H:%M"): b for b in data["bars"]}


def _t(hhmm: str, sec: int = 0) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, 27, h, m, sec, tzinfo=ET)


def sample_fills() -> list[Fill]:
    b = _bars()
    entry_short = b["13:50"]["close"]
    stop = entry_short + 20
    stop_bar = next(k for k in sorted(b) if k > "13:50" and b[k]["high"] >= stop)
    return [
        # 盘前一笔：用来验证"只复盘盘中"的过滤
        Fill(_t("08:45"), "MNQZ6", "Buy", 1, 21060.00),
        Fill(_t("08:58"), "MNQZ6", "Sell", 1, 21052.50),
        # 第1笔：开盘下杀后做多，分两次离场
        Fill(_t("10:05", 40), "MNQZ6", "Buy", 2, b["10:05"]["close"]),
        Fill(_t("10:50", 20), "MNQZ6", "Sell", 1, b["10:50"]["close"]),
        Fill(_t("11:30", 55), "MNQZ6", "Sell", 1, b["11:30"]["close"]),
        # 第2笔：午后区间上沿做空，被止损
        Fill(_t("13:50", 30), "MNQZ6", "Sell", 2, entry_short),
        Fill(_t(stop_bar, 10), "MNQZ6", "Buy", 2, stop),
    ]


SAMPLE_CHAT = (
    "- 有群友午盘指出：上午单边后 21,150 附近是今天的成交密集区，下午大概率在这附近震荡，不追单。\n"
    "- 有群友提醒：尾盘若站回 21,170 上方，留意向前日高点 21,290 附近的流动性推进。"
)
