"""Load server configuration without ever logging credentials."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv


def load_environment():
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False, encoding="utf-8-sig")
    if not os.environ.get("OPENAI_API_KEY") and os.environ.get("chatgpt_api"):
        os.environ["OPENAI_API_KEY"] = os.environ["chatgpt_api"]


def search_provider():
    provider = os.environ.get("SEARCH_PROVIDER", "").strip().lower()
    if provider:
        if provider not in {"openai", "codex"}:
            raise ValueError("SEARCH_PROVIDER должен быть openai или codex")
        return provider
    return "openai" if os.environ.get("OPENAI_API_KEY") or os.environ.get("chatgpt_api") else "codex"


def search_limit():
    return 5 if search_provider() == "openai" else 50
