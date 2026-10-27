"""Daily review pipeline.

    python -m reviewbot.main                 # 正式运行：今天（美东日期）
    python -m reviewbot.main --date 2026-10-27
    python -m reviewbot.main --demo          # 用示例数据跑通全流程（有 key 时会真的调用 Gemini）
    python -m reviewbot.main --demo --offline   # 示例数据 + 示例复盘，不调用 Gemini
    加 --no-push 只生成图片，不发 Discord
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import sample
from .card import render_card
from .config import load_config
from .gemini_review import build_prompt, demo_review, run_review
from .push import send_discord, send_text
from .trades import day_stats, describe_stats, describe_trades, filter_session, pair_trades


def get_fills(cfg: dict, day: str, demo: bool):
    if demo:
        return sample.sample_fills()
    raise NotImplementedError("成交数据抓取（Tradovate）还没接入，目前请用 --demo 测试。")


def get_chart(cfg: dict, day: str, symbols: list[str], demo: bool, out_dir: Path) -> Path | None:
    if demo:
        return cfg["_root"] / "samples/sample_chart.png"
    return None  # TradingView 截图在下一步接入


def get_chat_knowledge(cfg: dict, day: str, demo: bool) -> str:
    if demo:
        return sample.SAMPLE_CHAT
    return ""  # Discord 群聊在最后一步接入


def run(day: str | None, demo: bool, offline: bool, push: bool) -> int:
    cfg = load_config()
    root: Path = cfg["_root"]
    tz = cfg["session"]["timezone"]
    day = day or (sample.SAMPLE_DATE if demo else datetime.now(ZoneInfo(tz)).date().isoformat())
    out_dir = root / cfg.get("output_dir", "output") / day
    out_dir.mkdir(parents=True, exist_ok=True)
    webhook = cfg["discord_webhook_url"] if push else ""

    trades = pair_trades(get_fills(cfg, day, demo), cfg["point_value"], cfg.get("commission_per_side", 0))
    trades = filter_session(trades, tz, cfg["session"]["start"], cfg["session"]["end"])
    stats = day_stats(trades)

    if not trades:
        print(f"{day} 盘中没有交易。")
        if webhook and cfg.get("no_trade_action") == "notify":
            send_text(webhook, f"📭 {day} 美股盘中没有交易，今天不复盘。")
        return 0

    trades_text = describe_trades(trades, tz)
    stats_text = describe_stats(stats)
    chart = get_chart(cfg, day, stats["symbols"], demo, out_dir)
    chat = get_chat_knowledge(cfg, day, demo) or "（今天没有）"
    (out_dir / "trades.txt").write_text(stats_text + "\n\n" + trades_text, encoding="utf-8")

    if offline or not cfg["gemini_api_key"]:
        if not demo:
            raise RuntimeError("没有设置 GEMINI_API_KEY（.env 文件）")
        review, model_name = demo_review(root), "示例复盘（未调用 Gemini）"
    else:
        prompt = build_prompt(root / cfg["prompt_file"],
                              {"DATE": day, "STATS": stats_text, "TRADES": trades_text, "CHAT_KNOWLEDGE": chat})
        (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        review = run_review(cfg, prompt, chart.read_bytes() if chart else None)
        model_name = cfg["gemini"]["review_model"]
    (out_dir / "review.json").write_text(review.model_dump_json(indent=2), encoding="utf-8")

    card = render_card(root, out_dir / "card.png", day=day, trades=trades, stats=stats,
                       review=review, model_name=model_name, tz=tz)
    images = [p for p in (chart, card) if p]
    print(f"已生成：{card}")

    if webhook:
        sign = "🟢" if stats["total_usd"] > 0 else ("🔴" if stats["total_usd"] < 0 else "⚪")
        send_discord(webhook, f"{sign} **{day} 复盘** · {' / '.join(stats['symbols'])} · "
                              f"{stats['total_usd']:+,.2f} 美元 · {review.one_line}", images)
        print("已推送到 Discord")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    a = ap.parse_args()
    try:
        sys.exit(run(a.date, a.demo, a.offline, not a.no_push))
    except Exception as e:
        traceback.print_exc()
        cfg = load_config()
        if cfg["discord_webhook_url"] and not a.no_push:
            try:
                send_text(cfg["discord_webhook_url"], f"⚠️ 复盘机器人出错：{e}")
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
