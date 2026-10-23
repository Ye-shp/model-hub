"""Continuity: what a stopped attempt did, and the chat's plan.md."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import coordination
import sandbox
import store
import workspace as ws

from .config import ACTION_KINDS, NO_PROGRESS_RUNS, PLAN_FILE, PLAN_LIMIT
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


# ---- progress between automatic runs ----
# With no fixed limit on automatic continuations and phases, a chain stops when it stops changing anything: each
# automatic-capable run records a fingerprint of the chat's folder when it ends, and a run that left the folder as its
# parent left it (and shared nothing) counts as stalled.
_SKIP_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".cache", ".npm", ".pip"}
_HASH_BELOW = 1024 * 1024   # small files are fingerprinted by content (rewriting identical text isn't progress)
_MAX_FILES = 20000


def folder_mark(space: sandbox.Workspace) -> str:
    """A fingerprint of the chat's folder: every file's path and size, and the content of the small ones."""
    digest, root, count = hashlib.sha256(), Path(space.dir), 0
    for folder, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS)
        for name in sorted(files):
            if name == ".claude-brief.md":  # rewritten by every hand-off; says nothing about the work
                continue
            path = Path(folder) / name
            try:
                size = path.stat().st_size
                if path.is_symlink() or not path.is_file():
                    continue
                content = hashlib.sha256(path.read_bytes()).hexdigest() if size < _HASH_BELOW else str(path.stat().st_mtime_ns)
            except OSError:
                continue
            digest.update(f"{path.relative_to(root)}\0{size}\0{content}\n".encode())
            count += 1
            if count >= _MAX_FILES:
                return digest.hexdigest()
    return digest.hexdigest()


def _progress(job_id: str | None) -> dict | None:
    if not job_id:
        return None
    rows = ws.query("SELECT detail FROM events WHERE job_id=? AND kind='progress' ORDER BY id DESC LIMIT 1", (job_id,))
    try:
        return json.loads(rows[0]["detail"]) if rows else None
    except ValueError:
        return None


def record_progress(job: dict, space: sandbox.Workspace) -> dict:
    """Record (once per run) whether this run changed anything compared with the run before it in the chain."""
    found = _progress(job["id"])
    if found is not None:
        return found
    mark = folder_mark(space)
    before = _progress(job.get("parent"))
    shared = bool(ws.query("SELECT 1 FROM artifacts WHERE job_id=? LIMIT 1", (job["id"],)))
    found = {"mark": mark, "moved": bool(shared or before is None or before.get("mark") != mark)}
    ws.event(job["id"], "progress", json.dumps(found))
    return found


def stalled_runs(job: dict, space: sandbox.Workspace) -> int:
    """How many runs in a row, ending with this one, changed nothing."""
    if record_progress(job, space)["moved"]:
        return 0
    count, parent = 1, job.get("parent")
    while parent and count < 50:
        found = _progress(parent)
        if not found or found.get("moved"):
            break
        count += 1
        rows = ws.query("SELECT parent FROM jobs WHERE id=?", (parent,))
        parent = rows[0]["parent"] if rows else None
    return count


def stuck(job: dict, space: sandbox.Workspace) -> bool:
    """True when the chain has stopped making progress and should wait for the owner instead of going on."""
    stalled = stalled_runs(job, space)
    if stalled >= NO_PROGRESS_RUNS:
        ws.event(job["id"], "tool", f"No progress in the last {stalled} automatic run(s); waiting for you instead of "
                                    "continuing")
        return True
    return False
