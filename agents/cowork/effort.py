"""Reasoning effort per model call."""
from __future__ import annotations

import hub
from agents import ModelSettings


# Tools that only look at things: the call right after them rarely needs long thinking.
READ_ONLY = {"read_file", "list_files", "recall", "search_knowledge", "search_posts", "recent_posts", "topic_stats",
             "web_search", "read_webpage", "update_plan", "phone_screen"}


def _last_tool_names(items) -> set[str] | None:
    """Names of the tools whose results end the input, or None when the input doesn't end with tool results."""
    if not isinstance(items, list) or not items:
        return None
    names = {i.get("call_id"): i.get("name") for i in items if isinstance(i, dict) and i.get("type") == "function_call"}
    tail = []
    for item in reversed(items):
        if isinstance(item, dict) and item.get("type") == "function_call_output":
            tail.append(names.get(item.get("call_id"), ""))
            continue
        break
    return set(tail) if tail else None


def _failed(items) -> bool:
    for item in reversed(items):
        if not (isinstance(item, dict) and item.get("type") == "function_call_output"):
            break
        text = str(item.get("output", ""))[:200]
        if text.startswith(("Timed out", "An error occurred")) or (text.startswith("Exit code") and not text.startswith("Exit code 0")):
            return True
    return False


def effort_for(items, base: str) -> str:
    """Think hard when planning, after errors and after work happened; think briefly after just looking at things."""
    if base in ("none", "low"):
        return base
    tools = _last_tool_names(items)
    if tools and tools <= READ_ONLY and not _failed(items):
        return "low"
    return base


class AdaptiveModel(hub.StreamingModel):
    """Lowers reasoning effort for routine steps (see effort_for)."""
    async def get_response(self, system_instructions, input, model_settings, *args, **kwargs):
        extra = dict(model_settings.extra_body or {})
        base = extra.get("reasoning_effort")
        if base:
            extra["reasoning_effort"] = effort_for(input, base)
            model_settings = model_settings.resolve(ModelSettings(extra_body=extra))
        return await super().get_response(system_instructions, input, model_settings, *args, **kwargs)
