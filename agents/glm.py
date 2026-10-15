"""GLM, the hosted orchestrator for the owner's Cowork tasks.

A hosted GLM model (abliteration.ai's OpenAI-compatible API by default) leads the task: it thinks, plans, splits the
work into briefs and hands them to Qwen workers on both GPUs, then reviews what they made and writes the reply. The
Qwens do the actual work (shell, files, web, research, Claude Code, Codex, Jev), so GLM only spends tokens on
planning and reviewing. The owner connects it from a Cowork chat with `/connect glm <API key>` (or sets GLM_API_KEY
on the instance); the key stays root-only on the box and is never shown to a model.

The same key serves the "GLM" chat model on the chat site (openwebui_glm.py) and the vision model that reads video
frames for /frame (frames.py): the large GLM model takes no images, so pictures go to VISION_MODEL.

Spending is metered from the token usage of every call and capped per calendar month (`/connect glm budget <USD>`,
default GLM_MONTHLY_BUDGET_USD). When the key is missing, the budget is used up or GLM stops answering, Qwen leads
the task as before.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone

import store

URL = os.environ.get("GLM_BASE_URL", "https://api.abliteration.ai/v1").rstrip("/")
MODEL = os.environ.get("GLM_MODEL", "abliterated-model-large-v2")  # text only: it rejects image inputs
VISION_MODEL = os.environ.get("GLM_VISION_MODEL", "abliterated-model")  # same key; accepts images
# USD per million tokens: (input, cached input, output). abliteration.ai's listed rates, October 2026.
PRICE_INPUT = float(os.environ.get("GLM_PRICE_INPUT", "3.00"))
PRICE_CACHED = float(os.environ.get("GLM_PRICE_CACHED_INPUT", "0.30"))
PRICE_OUTPUT = float(os.environ.get("GLM_PRICE_OUTPUT", "5.00"))
PRICES = {MODEL: (PRICE_INPUT, PRICE_CACHED, PRICE_OUTPUT),
          VISION_MODEL: (float(os.environ.get("GLM_VISION_PRICE_INPUT", "1.00")),
                         float(os.environ.get("GLM_VISION_PRICE_CACHED_INPUT", "0.10")),
                         float(os.environ.get("GLM_VISION_PRICE_OUTPUT", "3.00")))}
DEFAULT_BUDGET = float(os.environ.get("GLM_MONTHLY_BUDGET_USD", "40"))
MAX_TOKENS = int(os.environ.get("GLM_MAX_TOKENS", "6000"))
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{19,299}")
_lock = threading.Lock()


def _key_file():
    return store.DATA / "glm-key"


def _settings_file():
    return store.DATA / "glm.json"


def _usage_file():
    return store.DATA / "glm-usage.json"


def _write_private(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(text)
    temporary.replace(path)


def _read_json(path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def api_key() -> str:
    path = _key_file()
    if path.is_file():
        return path.read_text().strip()
    return os.environ.get("GLM_API_KEY", "").strip()


def save_key(key: str) -> None:
    """The key from `/connect glm <key>`; "off" disconnects."""
    key = key.strip()
    if key.lower() == "off":
        _key_file().unlink(missing_ok=True)
        return
    if not KEY.fullmatch(key):
        raise ValueError("That doesn't look like an API key (20-300 letters, digits, '_', '-' or '.')")
    _write_private(_key_file(), key)


def budget() -> float:
    value = _read_json(_settings_file()).get("budget_usd")
    return float(value) if isinstance(value, (int, float)) and value >= 0 else DEFAULT_BUDGET


def save_budget(usd: float) -> None:
    if not 0 <= usd <= 100_000:
        raise ValueError("The monthly budget must be between 0 and 100000 USD")
    settings = _read_json(_settings_file())
    settings["budget_usd"] = round(float(usd), 2)
    _write_private(_settings_file(), json.dumps(settings))


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def this_month() -> dict:
    entry = _read_json(_usage_file()).get(_month()) or {}
    return {"usd": float(entry.get("usd", 0.0)), "calls": int(entry.get("calls", 0)),
            "input_tokens": int(entry.get("input_tokens", 0)), "cached_tokens": int(entry.get("cached_tokens", 0)),
            "output_tokens": int(entry.get("output_tokens", 0))}


def cost(input_tokens: int, cached_tokens: int, output_tokens: int, model: str = MODEL) -> float:
    price_in, price_cached, price_out = PRICES.get(model, PRICES[MODEL])  # unknown models bill at the higher rate
    cached = max(0, min(cached_tokens, input_tokens))
    return ((input_tokens - cached) * price_in + cached * price_cached + output_tokens * price_out) / 1_000_000


def record(input_tokens: int, cached_tokens: int, output_tokens: int, model: str = MODEL) -> float:
    """Adds one call to this month's meter; returns what it cost in USD."""
    spent = cost(input_tokens, cached_tokens, output_tokens, model)
    with _lock:
        usage = _read_json(_usage_file())
        month = _month()
        entry = usage.get(month) or {}
        entry["usd"] = round(float(entry.get("usd", 0.0)) + spent, 6)
        entry["calls"] = int(entry.get("calls", 0)) + 1
        entry["input_tokens"] = int(entry.get("input_tokens", 0)) + int(input_tokens)
        entry["cached_tokens"] = int(entry.get("cached_tokens", 0)) + int(cached_tokens)
        entry["output_tokens"] = int(entry.get("output_tokens", 0)) + int(output_tokens)
        usage[month] = entry
        # Keep a year of history.
        for old in sorted(usage)[:-12]:
            usage.pop(old, None)
        _write_private(_usage_file(), json.dumps(usage))
    return spent


def status() -> dict:
    month = this_month()
    return {"connected": bool(api_key()), "model": MODEL, "url": URL, "budget_usd": budget(),
            "spent_usd": round(month["usd"], 4), "calls": month["calls"]}


def available() -> str | None:
    """None if GLM can lead a task, otherwise why not."""
    if os.environ.get("COWORK_ORCHESTRATOR", "on").lower() in {"off", "false", "0"}:
        return "the GLM orchestrator is switched off on this instance (COWORK_ORCHESTRATOR=off)"
    if not api_key():
        return "GLM is not connected (send /connect glm <API key> in the chat)"
    if this_month()["usd"] >= budget():
        return f"this month's GLM budget is used up (${budget():.2f}; raise it with /connect glm budget <USD>)"
    return None


def client():
    from openai import AsyncOpenAI
    return AsyncOpenAI(base_url=URL, api_key=api_key(), timeout=300, max_retries=1)


def usage_numbers(usage) -> tuple[int, int, int]:
    """(input, cached input, output) tokens from a Chat Completions usage object."""
    if usage is None:
        return 0, 0, 0
    details = getattr(usage, "prompt_tokens_details", None)
    return (int(getattr(usage, "prompt_tokens", 0) or 0), int(getattr(details, "cached_tokens", 0) or 0),
            int(getattr(usage, "completion_tokens", 0) or 0))


def has_images(messages: list) -> bool:
    return any(isinstance(m.get("content"), list) and any(p.get("type") == "image_url" for p in m["content"]
                                                            if isinstance(p, dict)) for m in messages)


async def complete(messages: list, model: str = MODEL, max_tokens: int = MAX_TOKENS, **extra) -> tuple[str, float]:
    """One non-streamed call, metered: (reply text, cost in USD). Raises RuntimeError when GLM can't be used."""
    reason = available()
    if reason:
        raise RuntimeError(reason)
    async with client() as api:
        response = await api.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens, **extra)
    spent = record(*usage_numbers(response.usage), model=model)
    return (response.choices[0].message.content or "").strip(), spent


async def stream_chat(messages: list, model: str | None = None, max_tokens: int = 16000):
    """A metered, streamed chat for the chat site: yields {"reasoning": text}, {"content": text}, then
    {"done": {...}} or {"error": text}. Messages with images go to the vision model (the large one rejects them)."""
    reason = available()
    if reason:
        yield {"error": reason}
        return
    model = model or (VISION_MODEL if has_images(messages) else MODEL)
    usage = None
    try:
        async with client() as api:
            stream = await api.chat.completions.create(model=model, messages=messages, max_tokens=max_tokens, stream=True,
                                                       stream_options={"include_usage": True})
            async for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage
                for choice in chunk.choices:
                    delta = choice.delta
                    extra = delta.model_extra or {}  # abliteration.ai streams thoughts as "reasoning"
                    thought = getattr(delta, "reasoning_content", None) or extra.get("reasoning_content") or extra.get("reasoning")
                    if thought:
                        yield {"reasoning": thought}
                    if delta.content:
                        yield {"content": delta.content}
    except Exception as error:  # the API's message, never the key
        detail = getattr(error, "message", None) or str(error) or type(error).__name__
        yield {"error": f"{type(error).__name__}: {str(detail).replace(api_key(), '[key]')[:300]}"}
        return
    finally:
        if usage is not None:
            spent = record(*usage_numbers(usage), model=model)
    month = this_month()
    yield {"done": {"model": model, "cost_usd": round(spent if usage is not None else 0.0, 6),
                    "month_usd": round(month["usd"], 4), "budget_usd": budget()}}
