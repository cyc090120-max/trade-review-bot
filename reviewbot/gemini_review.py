"""Ask Gemini for a structured review: chart image + trade table + chat knowledge + prompt file."""
from __future__ import annotations

import json
import time
from pathlib import Path

from pydantic import BaseModel, Field


class EventComment(BaseModel):
    n: int = Field(description="出手/出场编号，与输入中的 ①②③ 一致（填数字 1、2、3）")
    verdict: str = Field(description="结论，只能是：执行到位 / 可以改进 / 不该做")
    tag: str = Field(description="贴在 K 线图上的短评，12 个字以内，说出这一下好或不好的关键原因")
    comment: str = Field(description="完整点评，2-3 句，指出具体 K 线或价格")


class Review(BaseModel):
    one_line: str = Field(description="一句话总结今天，20 字以内")
    market_context: str = Field(description="当天市场状态与关键位置，2-3 句")
    playbook_fit: str = Field(description="打法是否匹配当天状态，2-3 句")
    events: list[EventComment] = Field(description="每一次出手/出场的点评，覆盖所有编号，按编号顺序")
    chat_insights: list[str] = Field(description="与当天相关的群聊观点及印证/冲突，没有则为空列表")
    tomorrow: list[str] = Field(description="明日要点，最多 3 条，每条可直接执行")


def build_prompt(prompt_file: Path, values: dict[str, str]) -> str:
    text = prompt_file.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("%%")).strip()
    for key, val in values.items():
        text = text.replace("{{" + key + "}}", val)
    return text


def run_review(cfg: dict, prompt: str, chart_pngs: list[bytes]) -> Review:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=cfg["gemini_api_key"])
    parts: list = []
    for png in chart_pngs:
        parts.append(types.Part.from_bytes(data=png, mime_type="image/png"))
    parts.append(prompt)

    gcfg = types.GenerateContentConfig(
        temperature=cfg["gemini"].get("temperature", 0.4),
        response_mime_type="application/json",
        response_schema=Review,
    )
    last_err: Exception | None = None
    for attempt in range(4):
        try:
            resp = client.models.generate_content(
                model=cfg["gemini"]["review_model"], contents=parts, config=gcfg
            )
            if resp.parsed is not None:
                return resp.parsed
            return Review.model_validate_json(resp.text)
        except Exception as e:  # rate limit / transient server errors: back off and retry
            last_err = e
            time.sleep(10 * (attempt + 1))
    raise RuntimeError(f"Gemini 调用失败：{last_err}")


def demo_review(root: Path) -> Review:
    return Review.model_validate(json.loads((root / "samples/sample_review.json").read_text(encoding="utf-8")))
