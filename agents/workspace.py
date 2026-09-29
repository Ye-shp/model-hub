"""Durable projects, evidence, notes, artifacts and jobs. No model or cloud dependency."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from pathlib import Path
from contextlib import contextmanager

import store

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, brief TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notes (
  id INTEGER PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL,
  kind TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]', updated_at TEXT NOT NULL,
  UNIQUE(project, title)
);
CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY, project TEXT NOT NULL, title TEXT NOT NULL, source TEXT NOT NULL,
  content_hash TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(project, source, content_hash)
);
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge USING fts5(
  project UNINDEXED, document_id UNINDEXED, title, source UNINDEXED, chunk_no UNINDEXED, content,
  tokenize='unicode61'
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, project TEXT NOT NULL, task TEXT NOT NULL, skill TEXT NOT NULL,
  profile TEXT NOT NULL, status TEXT NOT NULL, result TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  allow_frontier INTEGER NOT NULL DEFAULT 0, allow_images INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, heartbeat_at TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_time ON jobs(status, created_at);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, project TEXT NOT NULL, job_id TEXT, name TEXT NOT NULL,
  path TEXT NOT NULL UNIQUE, media_type TEXT NOT NULL, created_at TEXT NOT NULL
);
"""

PROFILES = {
    # Reasoning tokens count against max_tokens, so these leave room to think *and* write a full
    # tool call (e.g. save_report with a Markdown deliverable) without truncating its JSON.
    # Measured on the live hub: a 3-script content job with writer + critic needs 10-15 minutes at ~33 tok/s.
    # Local models cost nothing per call, so these budgets bound time, not money.
    "fast": {"seconds": 300, "turns": 8, "subturns": 3, "tokens": 4096, "effort": "low"},
    "balanced": {"seconds": 900, "turns": 14, "subturns": 5, "tokens": 8192, "effort": "medium"},
    "deep": {"seconds": 1800, "turns": 20, "subturns": 6, "tokens": 12288, "effort": "medium"},
}


@contextmanager
def connection():
    db = store.connect()
    try:
        yield db
    finally:
        db.close()


def init() -> None:
    with connection() as db:
        db.executescript(SCHEMA)
        db.execute("INSERT OR IGNORE INTO projects VALUES (?,?,?,?)", ("default", "My workspace", "", store.now()))
        # Separate project for the Open WebUI Pipe, so invited friends never see the owner's own work.
        db.execute("INSERT OR IGNORE INTO projects VALUES (?,?,?,?)", ("friends", "Shared with invited friends", "", store.now()))
        present = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
        for name in ("thread", "requested_by"):  # added for Cowork chats; older databases gain them here
            if name not in present:
                db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT")
        db.commit()
    import coordination
    coordination.init()


def query(sql: str, params: tuple = ()) -> list[dict]:
    with connection() as db:
        return [dict(r) for r in db.execute(sql, params)]


def project_exists(project: str) -> bool:
    return bool(query("SELECT id FROM projects WHERE id=?", (project,)))


def create_project(name: str, brief: str = "") -> str:
    if not name.strip() or len(name) > 120 or len(brief) > 8000:
        raise ValueError("Use a project name up to 120 characters and brief up to 8000.")
    project = uuid.uuid4().hex[:12]
    with connection() as db, db:
        db.execute("INSERT INTO projects VALUES (?,?,?,?)", (project, name.strip(), brief, store.now()))
    return project


def save_note(project: str, title: str, content: str, kind: str = "fact", sources: list[str] | None = None) -> int:
    if not project_exists(project):
        raise ValueError("Unknown project")
    if kind not in {"fact", "decision", "preference", "checkpoint", "question"}:
        raise ValueError("Unknown memory kind")
    if not title.strip() or len(title) > 160 or len(content) > 12000:
        raise ValueError("Memory title/content exceeds its limit")
    with connection() as db, db:
        db.execute("""INSERT INTO notes(project,title,content,kind,sources,updated_at) VALUES (?,?,?,?,?,?)
          ON CONFLICT(project,title) DO UPDATE SET content=excluded.content,kind=excluded.kind,
          sources=excluded.sources,updated_at=excluded.updated_at""",
                   (project, title, content, kind, json.dumps(sources or []), store.now()))
        return db.execute("SELECT id FROM notes WHERE project=? AND title=?", (project, title)).fetchone()[0]


def memories(project: str, search: str = "", limit: int = 10) -> list[dict]:
    return query("SELECT * FROM notes WHERE project=? AND (title LIKE ? OR content LIKE ?) ORDER BY updated_at DESC, id DESC LIMIT ?",
                 (project, f"%{search}%", f"%{search}%", max(1, min(limit, 30))))


def chunks(text: str, size: int = 1800, overlap: int = 200):
    for start in range(0, len(text), size - overlap):
        yield text[start:start + size]


def ingest(project: str, title: str, text: str, source: str = "user supplied") -> dict:
    if not project_exists(project):
        raise ValueError("Unknown project")
    if not text.strip() or len(text) > 2_000_000 or len(title) > 200 or len(source) > 2000:
        raise ValueError("Supply non-empty text up to 2 million characters, a short title and source.")
    digest = hashlib.sha256(text.encode()).hexdigest()
    with connection() as db, db:
        existing = db.execute("SELECT id FROM documents WHERE project=? AND source=? AND content_hash=?", (project, source, digest)).fetchone()
        if existing:
            return {"id": existing[0], "duplicate": True}
        doc = uuid.uuid4().hex
        db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?)", (doc, project, title, source, digest, store.now()))
        for number, text_chunk in enumerate(chunks(text), 1):
            db.execute("INSERT INTO knowledge(project,document_id,title,source,chunk_no,content) VALUES (?,?,?,?,?,?)",
                       (project, doc, title, source, number, text_chunk))
    return {"id": doc, "duplicate": False}


def search(project: str, terms: str, limit: int = 6) -> list[dict]:
    words = re.findall(r"\w+", terms, re.UNICODE)[:20]
    if not words:
        return []
    match = " OR ".join('"' + w.replace('"', '""') + '"' for w in words)
    return query("SELECT document_id, title, source, chunk_no, content FROM knowledge WHERE knowledge MATCH ? AND project=? ORDER BY rank LIMIT ?",
                 (match, project, max(1, min(limit, 12))))


def document_chunk(project: str, document_id: str, chunk_no: int) -> list[dict]:
    return query("SELECT document_id,title,source,chunk_no,content FROM knowledge WHERE project=? AND document_id=? AND chunk_no=?",
                 (project, document_id, chunk_no))


def bounded_json(items: list[dict], limit: int = 12000) -> str:
    """Return complete records and explicit omissions, never a broken JSON substring."""
    kept = []
    for item in items:
        candidate = {"items": kept + [item], "omitted": len(items) - len(kept) - 1}
        if len(json.dumps(candidate, ensure_ascii=False)) > limit:
            break
        kept.append(item)
    return json.dumps({"items": kept, "omitted": len(items) - len(kept),
                       "hint": "Request fewer/specific records if omitted is nonzero."}, ensure_ascii=False)


def create_job(project: str, task: str, skill: str, profile: str = "balanced", allow_frontier: bool = False, allow_images: bool = False,
               thread: str | None = None, requested_by: str | None = None) -> str:
    from skills import load_skill
    load_skill(skill)
    if profile not in PROFILES or not project_exists(project):
        raise ValueError("Unknown project or speed profile")
    limit = 120000 if skill == "cowork" else 16000  # Cowork tasks carry the chat so far
    if not task.strip() or len(task) > limit:
        raise ValueError(f"Write a task up to {limit} characters")
    if thread is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", thread):
        raise ValueError("Invalid thread")
    job = uuid.uuid4().hex
    with connection() as db, db:
        db.execute("""INSERT INTO jobs(id,project,task,skill,profile,status,allow_frontier,allow_images,created_at,thread,requested_by)
          VALUES (?,?,?,?,?,'queued',?,?,?,?,?)""", (job, project, task, skill, profile, int(allow_frontier), int(allow_images), store.now(),
                                                   thread, (requested_by or "")[:200] or None))
    event(job, "queued", "Waiting for a worker")
    return job


def event(job: str, kind: str, detail: str) -> None:
    with connection() as db, db:
        db.execute("INSERT INTO events(job_id,kind,detail,created_at) VALUES (?,?,?,?)", (job, kind, detail[:2000], store.now()))


def claim_job() -> dict | None:
    with connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        # One job per project at a time prevents two agents overwriting a project's decisions. Cowork chats
        # each have their own thread (and folder), so different chats run side by side; one chat runs in order.
        row = db.execute("""SELECT * FROM jobs j WHERE status='queued' AND NOT EXISTS
            (SELECT 1 FROM jobs r WHERE r.project=j.project AND IFNULL(r.thread,'')=IFNULL(j.thread,'') AND r.status='running')
            ORDER BY created_at,rowid LIMIT 1""").fetchone()
        if not row:
            return None
        now = store.now()
        db.execute("UPDATE jobs SET status='running',started_at=?,heartbeat_at=?,attempts=attempts+1,error=NULL WHERE id=?", (now, now, row["id"]))
        return dict(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())


def finish_job(job: str, status: str, result: str = "", error: str = "") -> bool:
    if status not in {"completed", "failed", "interrupted"}:
        raise ValueError("Invalid final status")
    with connection() as db, db:
        changed = db.execute("UPDATE jobs SET status=?,result=?,error=?,finished_at=? WHERE id=? AND status='running'",
                             (status, result, error, store.now(), job)).rowcount
    if changed:
        event(job, status, error or "Result saved")
    return bool(changed)


def cancel_job(job: str) -> bool:
    with connection() as db, db:
        changed = db.execute("UPDATE jobs SET status='cancelled',finished_at=? WHERE id=? AND status IN ('queued','running')", (store.now(), job)).rowcount
    if changed:
        event(job, "cancelled", "Cancelled by owner")
    return bool(changed)


def resume_job(job: str) -> bool:
    with connection() as db, db:
        changed = db.execute("UPDATE jobs SET status='queued',finished_at=NULL,error=NULL WHERE id=? AND status IN ('failed','interrupted','cancelled')", (job,)).rowcount
    if changed:
        event(job, "resumed", "Resume requested; saved notes and artifacts are available")
    return bool(changed)


def recover_jobs() -> int:
    # Called only while holding the controller's exclusive lock. Never silently replay work.
    with connection() as db, db:
        return db.execute("UPDATE jobs SET status='interrupted',error='Worker stopped. Review saved progress, then Resume.' WHERE status='running'").rowcount


def write_artifact(project: str, job: str | None, name: str, content: bytes, media_type: str = "text/markdown") -> dict:
    if not project_exists(project):
        raise ValueError("Unknown project")
    clean = re.sub(r"[^a-zA-Z0-9._-]", "-", name)[:100].strip(".") or "result.md"
    identity = uuid.uuid4().hex
    folder = store.DATA / "artifacts" / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{identity}-{clean}"
    path.write_bytes(content)
    try:
        with connection() as db, db:
            db.execute("INSERT INTO artifacts VALUES (?,?,?,?,?,?,?)", (identity, project, job, clean, str(path.resolve()), media_type, store.now()))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return {"id": identity, "name": clean}


def snapshot(project: str) -> dict:
    projects = query("SELECT * FROM projects ORDER BY created_at")
    return {"projects": projects, "jobs": query("SELECT * FROM jobs WHERE project=? ORDER BY created_at DESC,rowid DESC LIMIT 60", (project,)),
            "memories": memories(project, limit=30),
            "documents": query("SELECT * FROM documents WHERE project=? ORDER BY created_at DESC LIMIT 100", (project,)),
            "artifacts": query("SELECT id,name,job_id,media_type,created_at FROM artifacts WHERE project=? ORDER BY created_at DESC,rowid DESC LIMIT 100", (project,)),
            "drafts": query("SELECT * FROM drafts WHERE project=? ORDER BY id DESC LIMIT 40", (project,)),
            "posts": query("SELECT COUNT(*) AS count FROM posts WHERE project=?", (project,))[0]["count"]}
