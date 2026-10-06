"""Jev, TypeSafe's System One model: typed judgments (yes/no, one-of, scored levels) with probabilities.

Cowork hands Jev the judgment work it would otherwise eyeball or prompt-and-parse: classifying, routing, ranking,
scoring against a rubric and checking claims, usually over many items. Jev doesn't write text, count, do arithmetic
or compare dates. The owner connects it from a Cowork chat with `/connect typesafe <API key>`; the key is kept
root-only on the box (or set TYPESAFE_API_KEY on the instance) and is never shown to the model.
Docs: https://docs.typesafe.ai (API: POST https://api.typesafe.ai/v1/systemone).
"""
from __future__ import annotations

import asyncio
import json
import os
import re

try:
    import httpx2 as httpx
except ImportError:
    import httpx

import store

URL = os.environ.get("TYPESAFE_URL", "https://api.typesafe.ai/v1/systemone")
MODEL = os.environ.get("TYPESAFE_MODEL", "jev-latest")
DAILY = int(os.environ.get("JEV_DAILY_REQUESTS", "2000"))
MAX_QUESTIONS = 40
MAX_STATE_CHARS = 100_000  # Jev's state budget is 32k tokens
KEY = re.compile(r"apikey_[A-Za-z0-9_]{20,300}")
QUESTION_ID = re.compile(r"[A-Za-z0-9_.-]{1,64}")
RETRY = {429, 500, 502, 503, 529}


def key_file():
    return store.DATA / "typesafe-key"


def api_key() -> str:
    path = key_file()
    if path.is_file():
        return path.read_text().strip()
    return os.environ.get("TYPESAFE_API_KEY", "").strip()


def save_key(key: str) -> None:
    key = key.strip()
    path = key_file()
    if key.lower() == "off":
        path.unlink(missing_ok=True)
        return
    if not KEY.fullmatch(key):
        raise ValueError("That doesn't look like a TypeSafe API key (it starts with apikey_)")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(key)


def used_today() -> int:
    import escalate
    return escalate.used_today("jev")


def status() -> dict:
    return {"installed": True, "signed_in": bool(api_key()), "used_today": used_today(), "daily_limit": DAILY}


def available() -> str | None:
    """None if usable, otherwise why not."""
    if not api_key():
        return "Jev is not connected (send /connect typesafe <API key> in the chat)"
    if used_today() >= DAILY:
        return f"Jev daily limit reached ({DAILY} requests per 24 hours)"
    return None


def check_questions(questions) -> dict:
    """The questions in TypeSafe's shape, or ValueError saying what to fix."""
    if not isinstance(questions, dict) or not questions:
        raise ValueError("questions must be an object of {id: question}")
    if len(questions) > MAX_QUESTIONS:
        raise ValueError(f"Ask at most {MAX_QUESTIONS} questions per call; split the rest into another call")
    for qid, question in questions.items():
        if not QUESTION_ID.fullmatch(str(qid)):
            raise ValueError(f"Question id {qid!r}: use letters, digits, _ . - (up to 64)")
        if not isinstance(question, dict) or question.get("type") not in {"noul", "choice", "score"}:
            raise ValueError(f"Question {qid}: type must be noul, choice or score")
        if not question.get("instructions"):
            raise ValueError(f"Question {qid}: instructions are required")
        criteria = question.get("criteria")
        if question["type"] == "choice" and not (isinstance(criteria, dict) and len(criteria) >= 2):
            raise ValueError(f"Question {qid}: a choice needs criteria as an object of at least 2 options")
        if question["type"] == "score" and not (isinstance(criteria, list) and 2 <= len(criteria) <= 10):
            raise ValueError(f"Question {qid}: a score needs criteria as a list of 2-10 levels, lowest first")
        if question["type"] == "noul" and criteria is not None and not isinstance(criteria, dict):
            raise ValueError(f"Question {qid}: noul criteria, if given, are {{'true': …, 'false': …}}")
    return questions


async def ask(state, questions: dict, transport=None, attempts: int = 3) -> dict:
    """One TypeSafe request: {"model", "answers", "usage"} or {"error"}."""
    reason = available()
    if reason:
        return {"error": reason}
    questions = check_questions(questions)
    if len(json.dumps(state, ensure_ascii=False)) > MAX_STATE_CHARS:
        return {"error": "state is too large for Jev (about 32k tokens); send only the relevant fields or split the items"}
    key = api_key()
    body = {"model": MODEL, "state": state, "questions": questions}
    async with httpx.AsyncClient(timeout=60, transport=transport) as client:
        for attempt in range(attempts):
            try:
                response = await client.post(URL, json=body, headers={"Authorization": f"Bearer {key}"})
            except httpx.HTTPError as error:
                if attempt + 1 == attempts:
                    return {"error": f"TypeSafe couldn't be reached ({type(error).__name__})"}
            else:
                if response.status_code == 200:
                    try:
                        data = response.json()
                    except ValueError:
                        return {"error": "TypeSafe returned a response that isn't JSON"}
                    return {"model": data.get("model"), "answers": data.get("answers") or {}, "usage": data.get("usage") or {}}
                if response.status_code not in RETRY or attempt + 1 == attempts:
                    detail = response.text[:500].replace(key, "[hidden]")
                    hint = {401: " (the API key was refused; reconnect with /connect typesafe)",
                            422: " (the request shape was rejected; fix the questions)"}.get(response.status_code, "")
                    return {"error": f"TypeSafe returned HTTP {response.status_code}{hint}: {detail}"}
            await asyncio.sleep(2 ** attempt)
    return {"error": "TypeSafe request failed"}


GUIDE = """JEV (ask_jev: TypeSafe's System One model, typed judgments with probabilities, cheap and fast)
Hand Jev the judgment work instead of eyeballing it or prompting yourself for JSON, whenever the task needs semantic
decisions you can state as questions, especially over many items:
- classify or route (which category/handler/platform/intent) -> choice; yes/no conditions (is it sarcastic, is it a
  question, does it mention a price, does the draft follow the brief) -> noul; grade along a described scale (hook
  strength, purchase intent, frustration, relevance to the brief) -> score.
- rank or filter: score each candidate (comments, posts, hooks, captions, search results, leads) on the same questions,
  then sort and threshold in code; check claims against their source before you rely on them.
- writing hooks, captions, titles, CTAs or post ideas: write 10-20 candidates, score them all in one ask_jev call (one
  score question per candidate against the brief), and keep the top few, instead of critiquing your own drafts in rounds.
How to ask: state = the item(s) as JSON with named fields, only what's relevant; questions = {id: {type, instructions,
criteria}}. choice criteria: {option: meaning}, include a none/other option when nothing may fit, and don't let option
order carry meaning. score criteria: 2-10 concrete levels, lowest first. Put several independent questions about the
same state in one call. For many items, one call per item or small batch (delegate_many or a loop of calls).
Jev does NOT write text, count, do arithmetic, compare dates or numbers, or follow double negatives; it answers literally
and treats the state as trustworthy. Do that part yourself or in code. Use the probabilities and confidence: treat
low-confidence answers as uncertain and say so; report results as evidence ("Jev rated 31/50 comments positive"), not
certainty. When the user asks to BUILD software with TypeSafe/Jev, hand the coding to Claude Code (ask_claude), which
has the TypeSafe skill installed and the key available as TYPESAFE_API_KEY."""
