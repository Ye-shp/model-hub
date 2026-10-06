"""Decisions the controller hands to Jev (TypeSafe) instead of a slow Qwen call or a brittle regex.

Each helper asks Jev typed questions and returns a number or verdict, or None when Jev isn't usable (not connected,
daily limit, error or timeout). None always means "fall back to what the hub did before Jev", so every decision here
works without Jev. Safety checks (approvals) only ever make the regex stricter, never looser.

Where it's used:
- kickoff.questions_for: skip the Qwen question-writer when the request clearly doesn't need questions.
- runner.direct_claude: does the message really ask Claude to do the work, and is it mainly a coding job for Claude.
- runner.run_job: is the final reply complete (one fix round if not), and is a stopped task finished (no
  automatic continuation if so).
- study.study (through research_tools): pre-screen a post before the full extraction.
- research_tools.publish_post and phone_link: confirm an approval the regex found isn't negated or hypothetical.
- trend_research passes the key to the last30days engine, whose reranker scores relevance with Jev (vendor
  lib/jev_rerank.py).
"""
from __future__ import annotations

import asyncio
import os
import re

import jev
import workspace as ws

from .continuity import current_request
from .tier import tier_for

TIMEOUT = float(os.environ.get("COWORK_JEV_TIMEOUT", "25"))
ENABLED = os.environ.get("COWORK_JEV_DECISIONS", "on").lower() not in {"off", "false", "0", "no"}
STATE_LIMIT = 24000  # characters of any one text field sent to Jev


def enabled(job: dict) -> bool:
    """Owner tasks that may use paid frontier models (the same switch as Claude Code), with Jev connected."""
    return (ENABLED and tier_for(job) == "owner" and bool(job.get("allow_frontier"))
            and jev.available() is None)


def _clip(text: str, limit: int = STATE_LIMIT) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit * 2 // 3] + "\n…\n" + text[-(limit // 3):]


async def ask(job: dict, purpose: str, state, questions: dict) -> dict | None:
    """Jev's answers by question id, or None. Logged as a Jev hand-off (counts toward JEV_DAILY_REQUESTS)."""
    if not enabled(job):
        return None
    ws.event(job["id"], "escalation", f"jev: {purpose}")
    try:
        async with asyncio.timeout(TIMEOUT):
            result = await jev.ask(state, questions, attempts=2)
    except (TimeoutError, ValueError) as error:
        result = {"error": type(error).__name__}
    ok = "answers" in result
    ws.event(job["id"], "escalation-done", f"jev: {purpose} {'finished' if ok else 'failed'}")
    return result["answers"] if ok else None


def noul(answers: dict | None, key: str) -> float | None:
    try:
        return float(answers[key]["noul"])
    except (TypeError, KeyError, ValueError):
        return None


def level(answers: dict | None, key: str) -> tuple[float, float] | None:
    """(score, confidence) of a score answer."""
    try:
        return float(answers[key]["score"]), float(answers[key].get("confidence") or 0)
    except (TypeError, KeyError, ValueError):
        return None


# ---- before starting ----
async def needs_questions(job: dict) -> float | None:
    """Probability that asking the user first would change what gets made."""
    task = job.get("task") or ""
    answers = await ask(job, "ask first?", {"conversation_and_request": _clip(task)}, {"ask": {
        "type": "noul",
        "instructions": "The text ends with the user's CURRENT REQUEST to an AI work assistant (earlier chat turns may "
                        "come before it). Would asking the user a clarifying question before starting substantially change "
                        "what gets made, because the request leaves open something only the user can settle (audience, "
                        "platform, format, length or scope, tone, which of several valid directions, which account or "
                        "file) and neither the request nor the earlier turns settle it?",
        "criteria": {"true": "Yes: an unanswered choice like that would change the result",
                     "false": "No: it's specific enough, small, a question to answer, small talk, a link to study, or "
                              "continuing earlier work"}}})
    return noul(answers, "ask")


async def claude_route(job: dict) -> tuple[str | None, bool]:
    """('asked' | 'code' | None, judged): whether the request goes to Claude Code first, and whether Jev decided it.
    'asked' = the current message asks Claude to do the work; 'code' = mainly a substantial coding job."""
    from .intent import wants_claude
    request = current_request(job.get("task") or "")
    names_claude = bool(re.search(r"\bclaude\b", request, re.IGNORECASE))
    auto = os.environ.get("COWORK_AUTO_CLAUDE", "on").lower() not in {"off", "false", "0", "no"}
    questions = {}
    if names_claude:
        questions["asked"] = {
            "type": "noul",
            "instructions": "Does this message ask Claude (Claude Code) to do some or all of the work? Only an instruction "
                            "for Claude to act counts: mentioning Claude as a topic, comparing it, or telling the "
                            "assistant not to use Claude does not.",
            "criteria": {"true": "Yes, it hands work to Claude", "false": "No, Claude is only mentioned"}}
    if auto:
        questions["code"] = {
            "type": "noul",
            "instructions": "Is this request mainly to build, write, fix, refactor or debug substantial software: an app, "
                            "website, tool, bot, scripts beyond a few lines, or changes across several code files? A quick "
                            "snippet, a coding question, research, writing, content or images is not.",
            "criteria": {"true": "Yes, mainly substantial software work", "false": "No"}}
    if not questions:
        return None, False
    answers = await ask(job, "route to Claude?", {"request": _clip(request)}, questions)
    if answers is None:
        return ("asked" if wants_claude(job.get("task") or "") else None), False
    asked = noul(answers, "asked")
    if names_claude and asked is not None and asked >= 0.5:
        return "asked", True
    if auto and (noul(answers, "code") or 0) >= 0.85:
        return "code", True
    if names_claude and asked is None:
        return ("asked" if wants_claude(job.get("task") or "") else None), False
    return None, True


# ---- after the work ----
async def reply_gap(job: dict, reply: str, files: list[str]) -> str | None:
    """None when the reply delivers the request (or Jev can't tell); otherwise a short note on what's missing."""
    request = current_request(job.get("task") or "")
    state = {"request": _clip(request, 12000), "reply": _clip(reply, 16000), "files_shared": files[:40]}
    answers = await ask(job, "is the reply complete?", state, {
        "complete": {
            "type": "score",
            "instructions": "How completely do the reply and the shared files deliver what the request asked for? A reply "
                            "that stops to ask the user for a decision or information only they have counts as complete "
                            "for that part.",
            "criteria": ["Doesn't deliver it: only a plan, an outline, instructions for the user, or an apology",
                         "Delivers a small part; most of what was asked is missing",
                         "Delivers about half; clear parts are missing",
                         "Delivers nearly all; minor gaps",
                         "Fully delivers what was asked"]},
        "promises": {
            "type": "noul",
            "instructions": "Does the reply say the assistant will do something later, or end with steps the assistant "
                            "itself should have done, instead of having done them?",
            "criteria": {"true": "Yes, it defers or hands back the assistant's own work", "false": "No"}}})
    complete = level(answers, "complete")
    defers = noul(answers, "promises")
    if complete is None:
        return None
    score, confidence = complete
    if score >= 2.5 and (defers or 0) < 0.8:
        return None
    if score >= 2.5 and confidence < 0.5:
        return None
    problems = []
    if score < 2.5:
        problems.append("it doesn't deliver everything the request asked for")
    if (defers or 0) >= 0.8:
        problems.append("it promises or hands back work instead of doing it")
    return ("A quality check of your reply found that " + " and ".join(problems) + ". Do the missing work now with your "
            "tools (or hand it to Claude Code where that fits), share the files, then write the final reply again. If "
            "something truly can't be done, say exactly what and why.")


async def finished(job: dict, report: str) -> float | None:
    """Probability that a stopped task's report shows the requested work is already done."""
    answers = await ask(job, "continue?", {"request": _clip(current_request(job.get("task") or ""), 8000),
                                           "report": _clip(report, 12000)}, {"done": {
        "type": "noul",
        "instructions": "The task stopped at a time or step limit and wrote this report. Does the report show the "
                        "requested work is finished, or that nothing useful is left for the assistant to do without the "
                        "user (for example it is waiting for the user's decision)?",
        "criteria": {"true": "Finished, or blocked on the user", "false": "Work the assistant can still do remains"}}})
    return noul(answers, "done")


# ---- research ----
async def useful_know_how(job: dict, material: str) -> float | None:
    """Probability that a post contains specific, actionable know-how worth a full study."""
    answers = await ask(job, "worth studying?", {"post": _clip(material, 20000)}, {"useful": {
        "type": "noul",
        "instructions": "Does this post (text, transcript, on-screen text, caption and comments) contain specific, "
                        "actionable know-how about UGC or creator ads, go-to-market, growth, content strategy or "
                        "short-form video, distribution, paid ads, sales, pricing, monetisation or running a creator "
                        "business: steps, numbers, scripts, hooks or tactics someone could reuse?",
        "criteria": {"true": "Yes, it has reusable tactics", "false": "No: entertainment, news, or opinion without tactics"}}})
    return noul(answers, "useful")


# ---- approvals (only ever stricter than the regex that found them) ----
async def confirms_approval(job: dict, message: str, action: str) -> bool | None:
    """Whether the user's message really approves `action` now (not negated, conditional or hypothetical)."""
    answers = await ask(job, "approval check", {"user_message": _clip(message, 8000)}, {"approves": {
        "type": "noul",
        "instructions": f"Does this message give the assistant clear permission to {action} now? Negated ('don't post "
                        "it'), conditional ('post it if…'), future ('we'll post it later'), questions ('should I post "
                        "it?') and quoted or reported text are not permission.",
        "criteria": {"true": "Yes, clear permission now", "false": "No"}}})
    probability = noul(answers, "approves")
    return None if probability is None else probability >= 0.7
