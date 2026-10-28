"""Daily review pipeline.

    python -m reviewbot.main                    # 正式运行：今天（美东日期）
    python -m reviewbot.main --date 2026-10-27
    python -m reviewbot.main --demo             # 用示例数据跑通全流程（有 key 时会真的调用 Gemini）
    python -m reviewbot.main --demo --offline   # 示例数据 + 示例复盘，不调用 Gemini
    加 --no-push 只生成图片，不发 Discord

Flow: fills -> trades -> numbered entry/exit events -> plain chart (numbers only) ->
Gemini looks at the chart and comments on every number -> final chart with the
comments next to the candles + summary card -> Discord.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import sample, session
from .card import render_card
from .chart import render_chart
from .config import load_config
from .events import build_events, describe_day
from .gemini_review import EventComment, Review, build_prompt, demo_review, run_review
from .market import DayBars, check_fills_in_range, fetch_day, load_sample
from .push import send_discord, send_text
from .trades import day_stats, describe_stats, filter_session, pair_trades


def get_fills(cfg: dict, day: str, demo: bool):
    if demo:
        return sample.sample_fills()
    session.require(cfg["_root"], "tradovate")  # 登录失效时报错，出错消息会推送到 Discord 提醒重新登录
    raise NotImplementedError("成交数据抓取（Tradovate）还没接入，目前请用 --demo 测试。")


def get_bars(cfg: dict, day: str, symbols: list[str], demo: bool) -> tuple[dict[str, DayBars], list[str]]:
    s = cfg["session"]
    out, problems = {}, []
    for sym in symbols:
        try:
            out[sym] = load_sample(cfg["_root"]) if demo else fetch_day(sym, day, s["timezone"], s["start"], s["end"])
        except Exception as e:  # the review still goes out, just without this chart
            problems.append(f"{sym} 行情获取失败：{e}")
    return out, problems


def get_chat_knowledge(cfg: dict, day: str, demo: bool) -> str:
    if demo:
        return sample.SAMPLE_CHAT
    return ""  # Discord 群聊在最后一步接入


def complete_review(review: Review, numbers: list[int]) -> Review:
    """Make sure every entry/exit number has exactly one comment, in order."""
    by_n = {c.n: c for c in review.events if c.n in numbers}
    review.events = [by_n.get(n) or EventComment(n=n, verdict="未点评", tag="AI 未给出点评", comment="AI 这次没有点评这一条。")
                     for n in numbers]
    return review


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

    events = build_events(trades)
    day_text = describe_day(trades, events, tz)
    stats_text = describe_stats(stats)
    (out_dir / "trades.txt").write_text(stats_text + "\n\n" + day_text, encoding="utf-8")

    bars, problems = get_bars(cfg, day, stats["symbols"], demo)
    for sym, db in bars.items():
        warn = check_fills_in_range(db, [e.price for e in events if e.symbol == sym])
        if warn:
            problems.append(f"{sym} {warn}")

    # 1) plain charts with numbered fills, for Gemini to look at
    def chart_for(sym: str, review: Review | None, name: str) -> Path:
        sym_events = [e for e in events if e.symbol == sym]
        return render_chart(root, out_dir / f"{name}_{sym}.png", day=day, day_bars=bars[sym], events=sym_events,
                            trades=trades, review=review, tz=tz)

    plain = [chart_for(sym, None, "plain") for sym in bars]

    # 2) review
    if offline or not cfg["gemini_api_key"]:
        if not demo:
            raise RuntimeError("没有设置 GEMINI_API_KEY（.env 文件）")
        review, model_name = demo_review(root), "示例复盘（未调用 Gemini）"
    else:
        chat = get_chat_knowledge(cfg, day, demo) or "（今天没有）"
        prompt = build_prompt(root / cfg["prompt_file"],
                              {"DATE": day, "STATS": stats_text, "TRADES": day_text, "CHAT_KNOWLEDGE": chat})
        (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        review = run_review(cfg, prompt, [p.read_bytes() for p in plain])
        model_name = cfg["gemini"]["review_model"]
    review = complete_review(review, [e.n for e in events])
    (out_dir / "review.json").write_text(review.model_dump_json(indent=2), encoding="utf-8")

    # 3) final images: charts with comments next to the candles, then the summary card
    charts = [chart_for(sym, review, "chart") for sym in bars]
    card = render_card(root, out_dir / "card.png", day=day, trades=trades, stats=stats,
                       review=review, model_name=model_name, tz=tz)
    print(f"已生成：{', '.join(str(p) for p in charts + [card])}")
    for msg in problems:
        print("注意：" + msg)

    if webhook:
        sign = "🟢" if stats["total_usd"] > 0 else ("🔴" if stats["total_usd"] < 0 else "⚪")
        text = (f"{sign} **{day} 复盘** · {' / '.join(stats['symbols'])} · "
                f"{stats['total_usd']:+,.2f} 美元 · {review.one_line}")
        if problems:
            text += "\n" + "\n".join(f"⚠️ {m}" for m in problems)
        send_discord(webhook, text, charts + [card])
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
