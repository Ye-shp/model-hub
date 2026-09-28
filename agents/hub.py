"""Connection to your model gateway, shared by every agent script."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path

from agents import OpenAIChatCompletionsModel, set_tracing_disabled
from openai import AsyncOpenAI, OpenAI

HERE = Path(__file__).resolve().parent


def load_env(path: Path = HERE / ".env") -> None:
    """Minimal .env reader so no extra package is needed."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


load_env()
# The Agents SDK uploads traces to OpenAI by default. Keep everything on your own hardware.
set_tracing_disabled(True)

HUB_URL = os.environ.get("HUB_URL", "").rstrip("/")
HUB_KEY = os.environ.get("HUB_KEY", "")
FRONTIER_MODEL = os.environ.get("FRONTIER_MODEL", "")


def _require() -> None:
    if not HUB_URL or not HUB_KEY:
        raise SystemExit("Set HUB_URL and HUB_KEY in agents/.env first (see agents/.env.example).")


def sync_client() -> OpenAI:
    _require()
    return OpenAI(base_url=HUB_URL, api_key=HUB_KEY, timeout=1800, max_retries=2)


def async_client() -> AsyncOpenAI:
    _require()
    return AsyncOpenAI(base_url=HUB_URL, api_key=HUB_KEY, timeout=1800, max_retries=2)


def model(name: str = "qwen") -> OpenAIChatCompletionsModel:
    """An Agents-SDK model backed by the gateway. "qwen" = least busy resident."""
    return OpenAIChatCompletionsModel(model=name, openai_client=async_client())


def image_part(png: bytes, max_side: int = 1280) -> dict:
    """Shrink a screenshot (fewer image tokens, faster) and wrap it as an inline image."""
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}
