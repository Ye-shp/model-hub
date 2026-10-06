"""Before a new request starts: decide whether to ask the user anything first, and ask it in one go.

Left to itself the lead agent almost never calls ask_user, so the decision is made here instead. With Jev connected,
Jev first decides whether questions are needed at all (most requests: no, so no model call); otherwise, or when they
are, one short model call (on the helper GPU, so the lead's GPU isn't held up) writes up to three questions or none. The questions go to the chat as one message; the task waits for the reply (or carries on with its own
assumptions when none comes) and then starts with the answers in hand.
"""
from __future__ import annotations

import asyncio
import json
import re

from agents import Agent, ModelSettings, Runner

import asking
import hub
import workspace as ws

from .intent import may_ask_first

MAX_QUESTIONS = 3
WAIT_MINUTES = 30
SKIP_BELOW = 0.35  # Jev's probability that questions would change the result, under which none are asked
PROMPT = """You decide whether an AI work assistant should ask its user anything BEFORE starting a request.
Read the conversation and the current request. Ask when the answer would really change what gets made and the user
hasn't said it: who the audience is, the platform or format, length or scope, tone or style, which of several valid
directions, a budget or deadline, which account, file or project they mean, or what "done" looks like for something
big or open-ended. Ask nothing for: small talk, quick factual questions, requests that are already specific, links
sent to be studied, continuing earlier work, or anything the assistant can sensibly decide or look up itself.
Ask at most 3 short questions, most important first. Give 2-5 short options for a question when the likely answers
are clear. Never ask permission to do what was requested.

Reply with JSON only, no other text:
{"questions": [{"question": "...", "options": ["...", "..."]}]}
or {"questions": []} when nothing needs asking."""


def _parse(text: str) -> list[dict]:
    text = re.sub(r"<think>[\s\S]*?</think>", "", text or "")
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return []
    found = []
    for item in (data.get("questions") or []) if isinstance(data, dict) else []:
        if len(found) >= MAX_QUESTIONS:
            break
        if isinstance(item, str):
            item = {"question": item}
        if not isinstance(item, dict) or not str(item.get("question") or "").strip():
            continue
        options = [str(o).strip()[:120] for o in (item.get("options") or []) if str(o).strip()][:5]
        found.append({"question": str(item["question"]).strip()[:400], "options": options})
    return found


def message(questions: list[dict]) -> tuple[str, list[str]]:
    """One chat message for the questions, and the options when there is a single question (tap-to-answer)."""
    if len(questions) == 1:
        return "Before I start: " + questions[0]["question"], questions[0]["options"]
    lines = ["Before I start, a few quick questions (answer any or all; I'll decide the rest):"]
    for n, item in enumerate(questions, 1):
        lines.append(f"{n}. {item['question']}" + (f" ({' / '.join(item['options'])})" if item["options"] else ""))
    return "\n".join(lines), []


async def questions_for(job: dict, client, gate, model: str) -> list[dict]:
    """Questions worth asking before starting, or [] (also on any failure: asking is never worth failing a task)."""
    task = job.get("task") or ""
    if not may_ask_first(task):
        return []
    # Jev answers "does this need questions at all?" in seconds; the Qwen call below (which writes the questions)
    # then only runs when it does. Without Jev, Qwen decides both.
    from . import judge
    chance = await judge.needs_questions(job)
    if chance is not None and chance < SKIP_BELOW:
        ws.event(job["id"], "tool", "No questions needed; starting")
        return []
    decider = Agent(name="kickoff", model=hub.model(model, client, gate), instructions=PROMPT,
                    model_settings=ModelSettings(max_tokens=2048, include_usage=True, extra_body={"reasoning_effort": "low"}))
    try:
        async with asyncio.timeout(150):
            result = await Runner.run(decider, task[-30000:], max_turns=1)
        return _parse(str(result.final_output))
    except Exception as error:  # noqa: BLE001
        ws.event(job["id"], "tool", f"Skipped the kickoff questions ({type(error).__name__})")
        return []


async def clarify(job: dict, client, gate, model: str, still_running) -> str:
    """Ask before starting when it matters. Returns text to add to the task ('' when nothing was asked)."""
    found = await questions_for(job, client, gate, model)
    if not found:
        return ""
    text, options = message(found)
    asked = asking.ask(job["id"], text, options, WAIT_MINUTES)
    ws.event(job["id"], "tool", "Waiting for your answers before starting")
    result = await asking.wait(asked["id"], still_running)
    asked_lines = "\n".join(f"- {q['question']}" for q in found)
    if result["status"] == "answered":
        ws.event(job["id"], "tool", "Got your answers; starting")
        return ("\n\nBEFORE STARTING YOU ASKED THE USER:\n" + asked_lines + "\nTHEIR ANSWER:\n" + result["answer"] +
                "\nFollow these answers. Anything they didn't answer, decide yourself and say what you chose.")
    ws.event(job["id"], "tool", "No answer; starting with my own assumptions")
    return ("\n\nBEFORE STARTING YOU ASKED THE USER (no answer came, so decide these yourself and state your choices "
            "at the top of your final reply):\n" + asked_lines)

