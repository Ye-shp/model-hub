"""
title: GLM
description: Chat with GLM (abliteration.ai) on the hub, through the box's stored key and monthly budget. Pictures go to its vision model.
author: Model Hub
version: 1.0.0
"""
import json

try:
    import httpx2 as httpx
except ImportError:
    import httpx
from pydantic import BaseModel, Field

HELP = """**GLM** — chat with the hosted GLM model right here. The key and the monthly budget live on the box
(`/connect glm <API key>`, `/connect glm budget <USD>`, `/connections` in Qwen Cowork). Pictures you paste go to its vision
model, since the large GLM model doesn't take images. This is plain chat: for work with files, the web and helpers use
**Qwen Cowork**, which GLM leads."""


def reasoning_delta(text: str) -> str:
    """Open WebUI's native expandable Thinking output."""
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": {"reasoning_content": text},
                                             "finish_reason": None}]}) + "\n\n"


def clean_messages(messages: list, limit: int) -> list:
    """OpenAI-style messages for the controller: text and inline images only, newest kept within `limit` characters.
    Connection commands never leave the chat site."""
    kept, used = [], 0
    for message in reversed(messages or []):
        role = message.get("role")
        if role not in {"system", "user", "assistant"}:
            continue
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get("type") == "text" and part.get("text"):
                    parts.append({"type": "text", "text": part["text"]})
                elif part.get("type") == "image_url":
                    url = (part.get("image_url") or {}).get("url", "")
                    if url.startswith("data:image/"):
                        parts.append({"type": "image_url", "image_url": {"url": url}})
            content = parts
            size = sum(len(p.get("text", "")) for p in parts) + 2000 * sum(p["type"] == "image_url" for p in parts)
            text = " ".join(p.get("text", "") for p in parts)
        else:
            content = str(content or "")
            size, text = len(content), content
        if text.strip().lower().startswith("/connect"):
            content, size = "(connection command, hidden)", 30
        if not content:
            continue
        if kept and used + size > limit:
            break
        kept.append({"role": role, "content": content})
        used += size
    kept.reverse()
    # A picture in the newest message sends the chat to GLM's vision model; otherwise older pictures become a note,
    # so the conversation stays on the large model (it rejects images).
    newest = kept[-1]["content"] if kept else ""
    if not (isinstance(newest, list) and any(p["type"] == "image_url" for p in newest)):
        for message in kept:
            if isinstance(message["content"], list):
                texts = [p["text"] for p in message["content"] if p["type"] == "text"]
                pictures = sum(p["type"] == "image_url" for p in message["content"])
                message["content"] = "\n".join(texts + ([f"[{pictures} image(s) shared earlier]"] if pictures else []))
    return kept


class Pipe:
    class Valves(BaseModel):
        CONTROLLER_URL: str = Field(default="http://127.0.0.1:8787", description="Agent controller, from the chat site's server")
        OWNER_KEY: str = Field(default="", description="Controller owner key (set by the hub on start)")
        HISTORY_CHARACTERS: int = Field(default=200000, ge=2000, le=800000, description="How much of the chat is sent along")

    def __init__(self):
        self.valves = self.Valves()

    def _client(self):
        return httpx.AsyncClient(base_url=self.valves.CONTROLLER_URL.rstrip("/"),
                                 headers={"Authorization": "Bearer " + self.valves.OWNER_KEY},
                                 timeout=httpx.Timeout(600, connect=10))

    async def pipe(self, body: dict, __user__: dict = None, __task__: str = None, __event_emitter__=None, **_):
        if __task__:  # titles, tags, follow-ups: no paid call for these
            yield "GLM chat"
            return
        if (__user__ or {}).get("role") != "admin":
            yield "Only the owner can use this model."
            return
        if len(self.valves.OWNER_KEY) < 32:
            yield "GLM isn't configured yet (missing controller key). Restart the hub."
            return
        messages = body.get("messages") or []
        last = messages[-1].get("content") if messages else ""
        text = last if isinstance(last, str) else " ".join(p.get("text", "") for p in last or [] if p.get("type") == "text")
        command = text.strip().lower()
        if command in {"/help", "help"}:
            yield HELP
            return
        if command.startswith("/connect"):
            yield "Send connection commands in **Qwen Cowork** (`/connect glm <API key>`), so keys never reach a model."
            return
        stream = body.get("stream", True)
        thinking, done = False, None
        try:
            async with self._client() as client:
                async with client.stream("POST", "/api/glm/chat",
                                         json={"messages": clean_messages(messages, self.valves.HISTORY_CHARACTERS)}) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode(errors="replace")[:300]
                        yield f"GLM couldn't answer (controller HTTP {response.status_code}): {detail}"
                        return
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        event = json.loads(line)
                        if "reasoning" in event:
                            if stream:
                                thinking = True
                                yield reasoning_delta(event["reasoning"])
                        elif "content" in event:
                            yield event["content"]
                        elif "error" in event:
                            yield ("\n\n" if thinking else "") + f"⚠️ GLM couldn't answer: {event['error']}"
                            return
                        elif "done" in event:
                            done = event["done"]
        except (httpx.HTTPError, ValueError) as error:
            yield f"\n\n⚠️ Lost contact with the hub's controller ({type(error).__name__}). Try again in a moment."
            return
        if done and __event_emitter__:
            await __event_emitter__({"type": "status", "data": {"done": True, "description": (
                f"{done['model']} · ${done['cost_usd']:.4f} · ${done['month_usd']:.2f} of ${done['budget_usd']:.2f} this month")}})
