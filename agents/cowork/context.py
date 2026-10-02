"""Context trimming: keep recent tool results whole, shrink old ones, before every model call."""
from __future__ import annotations

import json

from agents import RunConfig

from .config import HARD_CHARS, SOFT_CHARS

LEVELS = ((6, 1500, 1200), (3, 600, 500), (1, 300, 240))  # (recent results kept whole, old output chars, old argument chars)


def describe(command: str, limit: int = 90) -> str:
    command = " ".join(command.split())
    return command if len(command) <= limit else command[:limit - 1] + "…"

def _size(item) -> int:
    return len(json.dumps(item, ensure_ascii=False, default=str))


def _cut(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    return text[:head] + f"\n… [{len(text) - limit:,} characters trimmed] …\n" + text[-(limit - head):]


def _shrink_arguments(arguments: str, limit: int) -> str:
    """Shorten long strings inside a tool call's JSON arguments, keeping it valid JSON."""
    if not isinstance(arguments, str) or len(arguments) <= limit:
        return arguments
    try:
        data = json.loads(arguments)
    except ValueError:
        return json.dumps({"note": f"[arguments trimmed: {len(arguments):,} characters]"})
    per_value = max(120, limit // 2)

    def cut(value):
        if isinstance(value, str):
            return _cut(value, per_value)
        if isinstance(value, dict):
            return {k: cut(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cut(v) for v in value]
        return value
    return json.dumps(cut(data), ensure_ascii=False)


def _shrink_output(output, limit: int):
    text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return output
    return ("[Older tool result trimmed to save context. Re-run the command or re-read the file if you need the details.]\n"
            + _cut(text, limit))


def trim_items(items: list, soft: int = SOFT_CHARS, hard: int = HARD_CHARS) -> list:
    """A copy of the model input with old tool results and call arguments shortened until it fits."""
    total = sum(_size(i) for i in items)
    if total <= soft:
        return items
    items = [dict(i) if isinstance(i, dict) else i for i in items]
    for keep, output_limit, argument_limit in LEVELS:
        outputs = [n for n, i in enumerate(items) if isinstance(i, dict) and i.get("type") == "function_call_output"]
        calls = [n for n, i in enumerate(items) if isinstance(i, dict) and i.get("type") == "function_call"]
        for n in outputs[:-keep]:
            items[n]["output"] = _shrink_output(items[n].get("output", ""), output_limit)
        for n in calls[:-keep]:
            items[n]["arguments"] = _shrink_arguments(items[n].get("arguments", ""), argument_limit)
        # Old thinking is never needed again.
        reasoning = [n for n, i in enumerate(items) if isinstance(i, dict) and i.get("type") == "reasoning"]
        drop = set(reasoning[:-1])
        items = [i for n, i in enumerate(items) if n not in drop]
        total = sum(_size(i) for i in items)
        if total <= soft:
            return items
    if total > hard:
        # Last resort: shorten long messages (other than the most recent few items).
        for n, item in enumerate(items[:-4]):
            if isinstance(item, dict) and isinstance(item.get("content"), str) and len(item["content"]) > 20000:
                items[n]["content"] = _cut(item["content"], 20000)
    return items


def context_filter(data):
    from agents.run_config import ModelInputData
    return ModelInputData(input=trim_items(list(data.model_data.input)), instructions=data.model_data.instructions)


RUN_CONFIG = RunConfig(call_model_input_filter=context_filter)


def replayable(items: list) -> list:
    """Drop tool calls that never got a result (a stopped run can end mid-call), so the history can be sent again."""
    answered = {i.get("call_id") for i in items if isinstance(i, dict) and i.get("type") == "function_call_output"}
    called = {i.get("call_id") for i in items if isinstance(i, dict) and i.get("type") == "function_call"}
    return [i for i in items if not (isinstance(i, dict) and (
        (i.get("type") == "function_call" and i.get("call_id") not in answered) or
        (i.get("type") == "function_call_output" and i.get("call_id") not in called)))]
