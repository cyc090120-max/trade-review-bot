"""Load config.yaml (falls back to config.example.yaml) and .env secrets."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    load_dotenv(ROOT / ".env")
    path = ROOT / "config.yaml"
    if not path.exists():
        path = ROOT / "config.example.yaml"
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cfg["_root"] = ROOT
    cfg["gemini_api_key"] = os.getenv("GEMINI_API_KEY", "").strip()
    cfg["discord_webhook_url"] = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    return cfg
