"""GLM, the hosted orchestrator for the owner's Cowork tasks.

A hosted GLM model (abliteration.ai's OpenAI-compatible API by default) leads the task: it thinks, plans, splits the
work into briefs and hands them to Qwen workers on both GPUs, then reviews what they made and writes the reply. The
Qwens do the actual work (shell, files, web, research, Claude Code, Codex, Jev), so GLM only spends tokens on
planning and reviewing. The owner connects it from a Cowork chat with `/connect glm <API key>` (or sets GLM_API_KEY
on the instance); the key stays root-only on the box and is never shown to a model.

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
MODEL = os.environ.get("GLM_MODEL", "abliterated-model-large-v2")
# USD per million tokens (abliteration.ai's listed rates for the large models, October 2026).
PRICE_INPUT = float(os.environ.get("GLM_PRICE_INPUT", "3.00"))
PRICE_CACHED = float(os.environ.get("GLM_PRICE_CACHED_INPUT", "0.30"))
PRICE_OUTPUT = float(os.environ.get("GLM_PRICE_OUTPUT", "5.00"))
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


def cost(input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    cached = max(0, min(cached_tokens, input_tokens))
    return ((input_tokens - cached) * PRICE_INPUT + cached * PRICE_CACHED + output_tokens * PRICE_OUTPUT) / 1_000_000


def record(input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
    """Adds one call to this month's meter; returns what it cost in USD."""
    spent = cost(input_tokens, cached_tokens, output_tokens)
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
