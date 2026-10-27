"""Send images to a Discord channel through a webhook."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests


def send_discord(webhook_url: str, content: str, images: list[Path]) -> None:
    payload = {"content": content[:1900], "allowed_mentions": {"parse": []}}
    for attempt in range(4):
        handles = [open(p, "rb") for p in images]
        try:
            files = {f"files[{i}]": (p.name, h, "image/png") for i, (p, h) in enumerate(zip(images, handles))}
            r = requests.post(webhook_url, data={"payload_json": json.dumps(payload)}, files=files, timeout=60)
        finally:
            for h in handles:
                h.close()
        if r.status_code == 429:  # rate limited: wait as told
            time.sleep(float(r.json().get("retry_after", 5)))
            continue
        if r.ok:
            return
        if attempt == 3:
            raise RuntimeError(f"Discord 推送失败：{r.status_code} {r.text[:300]}")
        time.sleep(5)


def send_text(webhook_url: str, content: str) -> None:
    r = requests.post(webhook_url, json={"content": content[:1900], "allowed_mentions": {"parse": []}}, timeout=30)
    if not r.ok:
        raise RuntimeError(f"Discord 推送失败：{r.status_code} {r.text[:300]}")
