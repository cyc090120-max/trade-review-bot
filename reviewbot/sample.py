"""Demo data so the whole pipeline can run before any account is connected."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from .trades import Fill

ET = ZoneInfo("America/New_York")
SAMPLE_DATE = "2026-10-27"


def _t(hhmm: str) -> datetime:
    h, m = map(int, hhmm.split(":"))
    return datetime(2026, 10, 27, h, m, tzinfo=ET)


def sample_fills() -> list[Fill]:
    return [
        # 盘前一笔：用来验证"只复盘盘中"的过滤
        Fill(_t("08:45"), "MNQZ6", "Buy", 1, 21060.00),
        Fill(_t("08:58"), "MNQZ6", "Sell", 1, 21052.50),
        # ① 开盘下杀后做多，分两次止盈
        Fill(_t("10:05"), "MNQZ6", "Buy", 2, 21020.25),
        Fill(_t("10:50"), "MNQZ6", "Sell", 1, 21095.50),
        Fill(_t("11:30"), "MNQZ6", "Sell", 1, 21146.00),
        # ② 午后区间上沿做空，被止损
        Fill(_t("13:50"), "MNQZ6", "Sell", 2, 21147.00),
        Fill(_t("14:35"), "MNQZ6", "Buy", 2, 21167.00),
    ]


SAMPLE_CHAT = (
    "- 有群友午盘指出：上午单边后 21,150 附近是今天的成交密集区，下午大概率在这附近震荡，不追单。\n"
    "- 有群友提醒：尾盘若站回 21,170 上方，留意向前日高点 21,280 附近的流动性推进。"
)
