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

from . import sample, tradovate
from .card import render_card
from .chart import render_htf, render_lesson, render_overview, render_trade
from .context import describe_events, describe_levels
from .config import load_config
from .events import build_events, describe_day
from .gemini_review import EventComment, Review, build_prompt, demo_review, run_review
from .market import DayBars, check_fills_in_range, fetch_day, load_sample
from .push import send_discord, send_text
from .trades import day_stats, describe_stats, filter_session, pair_trades


def get_fills(cfg: dict, day: str, demo: bool):
    if demo:
        return sample.sample_fills()
    # 登录失效时抛出 LoginExpired，出错消息会推送到 Discord 提醒重新登录
    return tradovate.fetch_fills(cfg["_root"], day, cfg["session"]["timezone"])


def get_bars(cfg: dict, day: str, symbols: list[str], demo: bool) -> tuple[dict[str, DayBars], list[str]]:
    s = cfg["session"]
    out, problems = {}, []
    for sym in symbols:
        try:
            out[sym] = (load_sample(cfg["_root"]) if demo
                        else fetch_day(sym, day, s["timezone"], s["start"], s["end"], cfg.get("levels")))
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
        if db.note and not demo:
            problems.append(f"{sym} {db.note}")
    charted = [t for t in trades if t.symbol in bars]

    # 1) pictures Gemini looks at: the 1-hour background before the open, the day's 5 minutes,
    #    and a close-up of every trade (numbers only, no comments yet)
    htf = [render_htf(root, out_dir / f"htf_{sym}.png", day=day, db=db) for sym, db in bars.items() if db.htf]
    overview = [render_overview(root, out_dir / f"overview_{sym}.png", day=day, db=db, events=events,
                                trades=trades, tz=tz) for sym, db in bars.items()]
    plain = [render_trade(root, out_dir / f"plain_trade{t.n}.png", day=day, db=bars[t.symbol], trade=t,
                          events=events, review=None, tz=tz) for t in charted]
    image_list = "\n".join(
        [f"- 第{i}张：{p.stem.split('_')[1]} 1 小时图，开盘前的大周期背景" for i, p in enumerate(htf, 1)]
        + [f"- 第{len(htf) + i}张：{p.stem.split('_')[1]} 当天 5 分钟总览" for i, p in enumerate(overview, 1)]
        + [f"- 第{len(htf) + len(overview) + i}张：第{t.n}笔附近放大的 5 分钟图" for i, t in enumerate(charted, 1)])

    # 2) review
    if offline or not cfg["gemini_api_key"]:
        if not demo:
            raise RuntimeError("没有设置 GEMINI_API_KEY（.env 文件）")
        review, model_name = demo_review(root), "示例复盘（未调用 Gemini）"
    else:
        chat = get_chat_knowledge(cfg, day, demo) or "（今天没有）"
        prompt = build_prompt(root / cfg["prompt_file"], {
            "DATE": day, "STATS": stats_text, "TRADES": day_text, "CHAT_KNOWLEDGE": chat, "IMAGES": image_list,
            "LEVELS": "\n\n".join(describe_levels(db) for db in bars.values()),
            "EVENT_CONTEXT": describe_events(trades, events, bars, tz),
        })
        (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        review = run_review(cfg, prompt, [p.read_bytes() for p in htf + overview + plain])
        model_name = cfg["gemini"]["review_model"]
    review = complete_review(review, [e.n for e in events])
    (out_dir / "review.json").write_text(review.model_dump_json(indent=2), encoding="utf-8")

    # 3) final images: summary card, overview, background, then each trade with its comments
    closeups = [render_trade(root, out_dir / f"trade{t.n}.png", day=day, db=bars[t.symbol], trade=t,
                             events=events, review=review, tz=tz) for t in charted]
    lessons = []
    for k, lesson in enumerate(review.lessons, 1):
        db = bars.get(lesson.symbol) or next(iter(bars.values()))
        try:
            lessons.append(render_lesson(root, out_dir / f"lesson{k}.png", day=day, db=db, lesson=lesson, k=k,
                                         events=events, tz=tz))
        except Exception as e:  # a lesson with an odd time range shouldn't stop the review
            problems.append(f"今日教学 {k} 画图失败：{e}")
    card = render_card(root, out_dir / "card.png", day=day, trades=trades, stats=stats,
                       review=review, model_name=model_name, tz=tz)
    images = [card] + overview + htf + closeups + lessons
    print(f"已生成：{', '.join(str(p) for p in images)}")
    for msg in problems:
        print("注意：" + msg)

    if webhook:
        sign = "🟢" if stats["total_usd"] > 0 else ("🔴" if stats["total_usd"] < 0 else "⚪")
        text = (f"{sign} **{day} 复盘** · {' / '.join(stats['symbols'])} · "
                f"{stats['total_usd']:+,.2f} 美元 · {review.one_line}")
        if problems:
            text += "\n" + "\n".join(f"⚠️ {m}" for m in problems)
        send_discord(webhook, text, images)
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
