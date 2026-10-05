"""Questions a running Cowork task asks its user, and their answers.

The task's ask_user tool records a question and waits; the chat site (openwebui_cowork.py) and Telegram show it, and
the user's next message in that chat becomes the answer instead of a new task. One question per task is open at a
time. Unanswered questions expire, and the task carries on with its best assumption.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import store
import workspace as ws

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
  id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, question TEXT NOT NULL, options TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'pending', answer TEXT, asked_at TEXT NOT NULL, answered_at TEXT, wait_minutes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS questions_job ON questions(job_id, status);
"""
MAX_WAIT_MINUTES = 240
_clock = time.monotonic  # replaced in tests


def init():
    with ws.connection() as db:
        db.executescript(SCHEMA)


def _row(row) -> dict:
    item = dict(row)
    item["options"] = json.loads(item.get("options") or "[]")
    return item


def ask(job_id: str, question: str, options: list[str] | None = None, wait_minutes: int = 30) -> dict:
    question = re.sub(r"\s+\n", "\n", (question or "").strip())
    if not question or len(question) > 2000:
        raise ValueError("Ask one question of up to 2000 characters")
    options = [str(o).strip()[:200] for o in (options or []) if str(o).strip()][:6]
    wait_minutes = max(1, min(int(wait_minutes or 30), MAX_WAIT_MINUTES))
    with ws.connection() as db, db:
        db.execute("UPDATE questions SET status='replaced' WHERE job_id=? AND status='pending'", (job_id,))
        ident = db.execute("INSERT INTO questions(job_id,question,options,asked_at,wait_minutes) VALUES (?,?,?,?,?)",
                           (job_id, question, json.dumps(options), store.now(), wait_minutes)).lastrowid
    ws.event(job_id, "question", json.dumps({"id": ident, "question": question, "options": options}))
    return get(ident)


def get(question_id: int) -> dict | None:
    rows = ws.query("SELECT * FROM questions WHERE id=?", (question_id,))
    return _row(rows[0]) if rows else None


def pending(job_id: str) -> dict | None:
    rows = ws.query("SELECT * FROM questions WHERE job_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (job_id,))
    return _row(rows[0]) if rows else None


def pending_in_thread(project: str, thread: str) -> tuple[dict, dict] | None:
    """(job, question) for a running task in this chat that is waiting for the user."""
    rows = ws.query("""SELECT j.id AS job_id, q.id AS question_id FROM jobs j JOIN questions q ON q.job_id=j.id
                       WHERE j.project=? AND j.thread=? AND j.status='running' AND q.status='pending'
                       ORDER BY q.id DESC LIMIT 1""", (project, thread))
    if not rows:
        return None
    job = ws.query("SELECT * FROM jobs WHERE id=?", (rows[0]["job_id"],))[0]
    return job, get(rows[0]["question_id"])


def answer(job_id: str, text: str) -> dict:
    """Answer the task's open question. A bare option number ("2") picks that option."""
    text = (text or "").strip()
    if not text:
        raise ValueError("Write an answer")
    question = pending(job_id)
    if not question:
        raise ValueError("This task has no open question")
    options = question["options"]
    if options and re.fullmatch(r"\d{1,2}", text) and 1 <= int(text) <= len(options):
        text = options[int(text) - 1]
    with ws.connection() as db, db:
        changed = db.execute("UPDATE questions SET status='answered',answer=?,answered_at=? WHERE id=? AND status='pending'",
                             (text[:8000], store.now(), question["id"])).rowcount
    if not changed:
        raise ValueError("That question was already answered or has expired")
    ws.event(job_id, "answer", text[:500])
    return get(question["id"])


def expire(question_id: int) -> None:
    with ws.connection() as db, db:
        db.execute("UPDATE questions SET status='expired' WHERE id=? AND status='pending'", (question_id,))


async def wait(question_id: int, still_running, poll: float = 2.0) -> dict:
    """Wait for the answer (or the question's time limit). still_running() raises when the task was stopped."""
    question = get(question_id)
    deadline = _clock() + question["wait_minutes"] * 60
    while True:
        question = get(question_id)
        if question["status"] != "pending":
            return question
        if _clock() >= deadline:
            expire(question_id)
            return get(question_id)
        still_running()
        await asyncio.sleep(poll)


def show(question: dict) -> str:
    """The question as the user sees it, in chat or Telegram."""
    lines = ["❓ **Cowork has a question**", "", question["question"]]
    if question["options"]:
        lines += [""] + [f"{n}. {option}" for n, option in enumerate(question["options"], 1)]
        lines += ["", "Reply with a number or your own answer."]
    else:
        lines += ["", "Reply in this chat to answer."]
    lines.append(f"_The task is paused and waits up to {question['wait_minutes']} minutes; after that it continues with its "
                 "best assumption._")
    return "\n".join(lines)
