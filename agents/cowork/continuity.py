"""Continuity: what a stopped attempt did, and the chat's plan.md."""
from __future__ import annotations

import json
import re

import coordination
import sandbox
import store
import workspace as ws

from .config import ACTION_KINDS, PLAN_FILE, PLAN_LIMIT
from .context import _cut, describe


def current_request(task: str) -> str:
    # Earlier chat messages can quote this header too. The pipe appends its own
    # canonical header after the history, so only the last complete boundary wins.
    markers = list(re.finditer(r"(?:\A|(?:\r?\n){2})CURRENT REQUEST:\r?\n", task))
    return task[markers[-1].end():] if markers else task


_RESUME_REQUEST = re.compile(
    r"^(?:please\s+)?(?:continue\b|resume\b|keep\s+going\b|carry\s+on\b|go\s+on\b|pick\s+up\s+(?:where|from)\b)",
    re.IGNORECASE)
_RESUME_REFERENCES = {
    "a", "an", "the", "this", "that", "our", "my", "your", "previous", "old", "earlier", "last", "unfinished",
    "stopped", "interrupted", "work", "task", "project", "phase", "request", "from", "where", "you", "we", "it",
    "left", "off", "with", "on", "to", "and", "working", "doing", "now", "please",
}
_ACTIVITY_WORDS = {"explaining": "explain", "building": "build", "writing": "write", "fixing": "fix",
                   "making": "make", "researching": "research", "analyzing": "analyze", "analysing": "analyse"}


def _resume_applies(request: str, previous: dict) -> bool:
    match = _RESUME_REQUEST.match(request)
    if not match:
        return False

    def words(text):
        return {_ACTIVITY_WORDS.get(word, word) for word in re.findall(r"[\w-]+", text.casefold())}

    # "Continue" and "resume the work" refer to the preceding task. A specific
    # new subject ("continue explaining comets") must not revive an unrelated app.
    subject = words(request[match.end():]) - _RESUME_REFERENCES
    return not subject or subject <= words(current_request(previous.get("task") or ""))


def _previous_to_continue(job: dict) -> list[dict]:
    """A parent phase, or the preceding task when the user asks to resume it."""
    if not job.get("thread"):
        return []
    automatic = (job.get("task") or "").lstrip().startswith(("AUTOMATIC NEXT PHASE", "AUTOMATIC CONTINUATION"))
    if automatic:
        if not job.get("parent"):
            return []
        # Another request can be queued between phases. Continue this task's own
        # parent rather than accidentally adopting that intervening request.
        return ws.query("SELECT * FROM jobs WHERE id=? AND project=? AND thread=?",
                        (job["parent"], job["project"], job["thread"]))
    request = current_request(job.get("task") or "").strip()
    if not _RESUME_REQUEST.match(request):
        return []
    rows = ws.query("""SELECT * FROM jobs WHERE project=? AND thread=? AND id!=? AND created_at<=?
                       ORDER BY created_at DESC, rowid DESC LIMIT 1""",
                    (job["project"], job["thread"], job["id"], job.get("created_at") or store.now()))
    return rows if rows and _resume_applies(request, rows[0]) else []


def _attempt_summary(title: str, job: dict, events: list[dict], result: str = "") -> str:
    lines = [title, f"Status: {job['status']}" + (f" — {job['error']}" if job.get("error") else "")]
    lines.append("Request: " + _cut(current_request(job.get("task") or "").strip(), 1500))
    plan = coordination.plan(job["id"])
    if plan:
        marks = {"completed": "done", "in_progress": "was in progress", "pending": "not started"}
        lines.append("Plan: " + "; ".join(f"[{marks.get(s['status'], s['status'])}] {s['title']}" for s in plan))
    actions = []
    for event in events:
        if event["kind"] not in ACTION_KINDS:
            continue
        detail = event["detail"]
        if event["kind"] == "artifact":
            try:
                detail = "Shared " + json.loads(detail)["name"]
            except (ValueError, KeyError):
                pass
        actions.append("- " + describe(detail, 160))
    if actions:
        lines.append(f"What it did ({len(actions)} actions{', last 40 shown' if len(actions) > 40 else ''}):")
        lines += actions[-40:]
    if result:
        lines.append("Its report:\n" + _cut(result, 3000))
    return "\n".join(lines)


def recap(job: dict) -> str:
    """Summaries of work this task should continue: an earlier attempt of it, or a stopped previous task in the chat."""
    parts = []
    if job.get("attempts", 1) > 1:
        starts = ws.query("SELECT id FROM events WHERE job_id=? AND kind='started' ORDER BY id", (job["id"],))
        if len(starts) >= 2:
            events = ws.query("SELECT kind,detail FROM events WHERE job_id=? AND id<? ORDER BY id", (job["id"], starts[-1]["id"]))
            previous = ws.query("SELECT * FROM jobs WHERE id=?", (job["id"],))[0]
            parts.append(_attempt_summary("AN EARLIER ATTEMPT OF THIS SAME TASK", {**previous, "status": "stopped"}, events))
    rows = _previous_to_continue(job)
    if rows:
        previous = rows[0]
        partial = ws.query("SELECT detail FROM events WHERE job_id=? AND kind='partial' LIMIT 1", (previous["id"],))
        stopped = previous["status"] in {"failed", "interrupted", "cancelled"} or partial
        if stopped or previous["id"] == job.get("parent"):
            events = ws.query("SELECT kind,detail FROM events WHERE job_id=? ORDER BY id", (previous["id"],))
            title = ("THE PREVIOUS TASK IN THIS CHAT STOPPED BEFORE FINISHING" if stopped else
                     "THE PREVIOUS PHASE OF THIS SAME WORK")
            parts.append(_attempt_summary(title, previous, events, previous.get("result") or ""))
    if not parts:
        return ""
    return ("\n\n".join(parts) + "\n\nContinue from where that work stopped: check the files it made (list_files) and don't "
            "redo finished steps unless their output is missing or wrong.")


def read_plan(space: sandbox.Workspace) -> str:
    path = space.dir / PLAN_FILE
    try:
        if path.is_file() and not path.is_symlink():
            return _cut(path.read_text(encoding="utf-8", errors="replace"), PLAN_LIMIT)
    except OSError:
        pass
    return ""


def chain_depth(job: dict) -> int:
    depth, parent = 0, job.get("parent")
    while parent and depth < 50:
        rows = ws.query("SELECT parent FROM jobs WHERE id=?", (parent,))
        depth += 1
        parent = rows[0]["parent"] if rows else None
    return depth
