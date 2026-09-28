"""Connection to your model gateway, shared by every agent script."""
from __future__ import annotations

import base64
import io
import os
from pathlib import Path

from agents import OpenAIChatCompletionsModel, ModelResponse, Usage, set_tracing_disabled
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
            os.environ.setdefault(key.strip(), value.strip().strip('\"').strip("'"))


load_env()
# The Agents SDK uploads traces to OpenAI by default. Keep everything on your own hardware.
set_tracing_disabled(True)

HUB_URL = os.environ.get("HUB_URL", "").rstrip("/")
HUB_KEY = os.environ.get("HUB_KEY", "")
FRONTIER_MODEL = os.environ.get("FRONTIER_MODEL", "")


def _require() -> None:
    if not HUB_URL or not HUB_KEY:
        raise RuntimeError("Set HUB_URL and HUB_KEY in agents/.env first (see agents/.env.example).")


def sync_client() -> OpenAI:
    _require()
    return OpenAI(base_url=HUB_URL, api_key=HUB_KEY, timeout=100, max_retries=0)


def async_client() -> AsyncOpenAI:
    _require()
    return AsyncOpenAI(base_url=HUB_URL, api_key=HUB_KEY, timeout=200, max_retries=0)


class StreamingModel(OpenAIChatCompletionsModel):
    """Consume SSE inside the SDK run loop, preserving tool calls and usage."""
    def __init__(self, name, client, gate=None, before_call=None):
        super().__init__(model=name, openai_client=client)
        self.gate, self.before_call = gate, before_call

    async def get_response(self, *args, **kwargs):
        async def consume():
            if self.before_call:
                self.before_call(self.model)
            response = None
            async for event in super(StreamingModel, self).stream_response(*args, **kwargs):
                if event.type == "response.completed":
                    response = event.response
            if response is None or not response.output:
                raise RuntimeError("Model stream ended without a complete response. Try a smaller task or output limit.")
            raw = response.usage
            usage = Usage(requests=1, input_tokens=raw.input_tokens, output_tokens=raw.output_tokens,
                          total_tokens=raw.total_tokens) if raw else Usage(requests=1)
            return ModelResponse(output=response.output, usage=usage, response_id=None)
        if self.gate:
            async with self.gate:
                return await consume()
        return await consume()


def model(name: str = "qwen", client=None, gate=None, before_call=None) -> OpenAIChatCompletionsModel:
    """An Agents-SDK model backed by the gateway. "qwen" = least busy resident."""
    return StreamingModel(name, client or async_client(), gate, before_call)


def image_part(png: bytes, max_side: int = 1280) -> dict:
    """Shrink a screenshot (fewer image tokens, faster) and wrap it as an inline image."""
    from PIL import Image

    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()}}
