"""Owner's local dashboard and job controller. python agents/console.py --port 8787"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import json
import os
from pathlib import Path
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import hub  # Reads agents/.env before the data directory is selected.
import access as cf_access
import store
import workspace as ws
import coordination
from skills import catalog
from worker import controller_lock, serve

WEB = Path(__file__).resolve().parents[1] / "console"


class ProjectIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    brief: str = Field(default="", max_length=8000)


class JobIn(BaseModel):
    project: str = "default"
    task: str = Field(min_length=1, max_length=120000)
    skill: str = "research-brief"
    profile: str = "balanced"
    allow_frontier: bool = False
    allow_images: bool = False
    thread: str | None = Field(default=None, max_length=80)
    requested_by: str | None = Field(default=None, max_length=200)


class TokenIn(BaseModel):
    token: str = Field(min_length=20, max_length=400)


class CodeIn(BaseModel):
    ref: str = Field(pattern=r"^[0-9a-f]{40}$")


class UploadIn(BaseModel):
    project: str
    thread: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    content_b64: str


class DocumentIn(BaseModel):
    project: str = "default"
    title: str = Field(min_length=1, max_length=200)
    source: str = Field(default="user supplied", max_length=2000)
    text: str = Field(min_length=1, max_length=2_000_000)


class NoteIn(BaseModel):
    project: str = "default"
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(max_length=12000)
    kind: str = "preference"


def owner_key() -> str:
    configured = os.environ.get("CONSOLE_KEY", "")
    if configured:
        if len(configured) < 32:
            raise ValueError("CONSOLE_KEY must be at least 32 characters")
        return configured
    store.DATA.mkdir(parents=True, exist_ok=True)
    path = store.DATA / "console.key"
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        key = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(key)
        return key


def create_app(key: str | None = None, run_worker: bool = True, runner=None) -> FastAPI:
    ws.init()
    token = key or owner_key()
    public_url = os.environ.get("CONSOLE_URL", "").rstrip("/")
    public_host = urlparse(public_url).hostname
    allowed_hosts = {"localhost", "127.0.0.1", "testserver"}
    if public_host:
        allowed_hosts.add(public_host)

    @asynccontextmanager
    async def lifespan(app):
        if not run_worker:
            yield
            return
        from crew import run_job
        with controller_lock():
            ws.recover_jobs()
            task = asyncio.create_task(serve(runner or run_job, lambda: bool(hub.HUB_URL and hub.HUB_KEY)))
            try:
                yield
            finally:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    app = FastAPI(title="Model Hub workspace", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def access(request: Request, call_next):
        host = urlparse("http://" + request.headers.get("host", "")).hostname
        if host not in allowed_hosts:
            return JSONResponse({"error": "Unexpected hostname"}, status_code=403)
        if request.url.path.startswith("/api/"):
            supplied = request.headers.get("authorization", "")
            owner = hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode())
            if not owner and cf_access.configured() and request.headers.get("cf-access-jwt-assertion"):
                # Signed in through Cloudflare Access as the owner: no key needed.
                owner = await asyncio.to_thread(cf_access.verify, request.headers["cf-access-jwt-assertion"]) is not None
            if not owner:
                return JSONResponse({"error": "Paste your workspace owner key to unlock."}, status_code=401)
            origin = request.headers.get("origin")
            if origin and origin not in {public_url, f"http://{request.headers.get('host')}", f"https://{request.headers.get('host')}"}:
                return JSONResponse({"error": "Unexpected origin"}, status_code=403)
            try:
                limit = 70_000_000 if request.url.path == "/api/workspace/upload" else 3_000_000
                if int(request.headers.get("content-length", "0")) > limit:
                    return JSONResponse({"error": "Upload is too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "Invalid content length"}, status_code=400)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
        return response

    @app.exception_handler(ValueError)
    async def bad_value(request, error):
        return JSONResponse({"error": str(error)}, status_code=400)

    @app.get("/")
    def home():
        return FileResponse(WEB / "index.html")

    @app.get("/app.js")
    def js():
        return FileResponse(WEB / "app.js", media_type="application/javascript")

    @app.get("/style.css")
    def css():
        return FileResponse(WEB / "style.css", media_type="text/css")

    @app.get("/api/state")
    def state(project: str = "default"):
        if not ws.project_exists(project):
            raise HTTPException(404, "Project not found")
        return {**ws.snapshot(project), "skills": catalog(), "profiles": ws.PROFILES, "attention": coordination.attention(project),
                "configured": bool(hub.HUB_URL and hub.HUB_KEY), "frontier_configured": bool(hub.FRONTIER_MODEL)}

    # ---- operations views: everything the agents do, across all projects ----
    @app.get("/api/overview")
    def overview():
        import escalate
        projects = ws.query("""SELECT p.id, p.name,
            (SELECT COUNT(*) FROM jobs j WHERE j.project=p.id) AS tasks,
            (SELECT COUNT(*) FROM jobs j WHERE j.project=p.id AND j.status IN ('queued','running')) AS active,
            (SELECT COUNT(*) FROM notes n WHERE n.project=p.id) AS memories,
            (SELECT COUNT(*) FROM artifacts a WHERE a.project=p.id) AS files FROM projects p ORDER BY p.created_at""")
        people = ws.query("""SELECT COALESCE(NULLIF(j.requested_by,''),'console') AS who, COUNT(*) AS tasks,
            SUM(j.status IN ('queued','running')) AS active, SUM(j.status='completed') AS completed,
            SUM(j.status IN ('failed','interrupted')) AS failed, MAX(j.created_at) AS last_task,
            GROUP_CONCAT(DISTINCT j.project) AS projects FROM jobs j GROUP BY who ORDER BY last_task DESC""")
        tokens = {}
        for row in ws.query("""SELECT COALESCE(NULLIF(j.requested_by,''),'console') AS who, e.detail FROM events e
                               JOIN jobs j ON j.id=e.job_id WHERE e.kind='usage'"""):
            try:
                usage = json.loads(row["detail"])
            except ValueError:
                continue
            total = tokens.setdefault(row["who"], {"input_tokens": 0, "output_tokens": 0, "model_calls": 0})
            total["input_tokens"] += usage.get("input_tokens") or 0
            total["output_tokens"] += usage.get("output_tokens") or 0
            total["model_calls"] += usage.get("requests") or 0
        for person in people:
            person.update(tokens.get(person["who"], {"input_tokens": 0, "output_tokens": 0, "model_calls": 0}))
        try:
            connections = escalate.status()
        except Exception as error:  # the sandbox users only exist on the hub box
            connections = {"error": f"{type(error).__name__}: {error}"}
        return {"projects": projects, "people": people, "connections": connections,
                "escalations": ws.query("""SELECT e.detail, e.created_at, j.requested_by FROM events e JOIN jobs j ON j.id=e.job_id
                                           WHERE e.kind IN ('escalation','escalation-done') ORDER BY e.id DESC LIMIT 30""")}

    @app.get("/api/activity")
    def activity(project: str = "all", limit: int = 100):
        where, params = ("", ()) if project == "all" else ("WHERE j.project=?", (project,))
        return {"jobs": ws.query(f"""SELECT j.id, j.project, p.name AS project_name, j.skill, j.profile, j.status, substr(j.task,1,400) AS task,
            j.requested_by, j.thread, j.created_at, j.started_at, j.finished_at, j.error,
            (SELECT COUNT(*) FROM events e WHERE e.job_id=j.id AND e.kind IN ('tool','delegate','escalation','image-request')) AS actions,
            (SELECT COUNT(*) FROM artifacts a WHERE a.job_id=j.id) AS files
            FROM jobs j JOIN projects p ON p.id=j.project {where} ORDER BY j.created_at DESC, j.rowid DESC LIMIT ?""",
            (*params, max(1, min(limit, 500))))}

    @app.get("/api/jobs/{job}/timeline")
    def timeline(job: str):
        rows = ws.query("SELECT * FROM jobs WHERE id=?", (job,))
        if not rows:
            raise HTTPException(404, "Task not found")
        return {**rows[0], "plan": coordination.plan(job),
                "events": ws.query("SELECT id,kind,detail,created_at FROM events WHERE job_id=? AND kind!='model' ORDER BY id LIMIT 2000", (job,)),
                "model_calls": ws.query("SELECT COUNT(*) AS n FROM events WHERE job_id=? AND kind='model'", (job,))[0]["n"],
                "artifacts": ws.query("SELECT id,name,media_type,created_at FROM artifacts WHERE job_id=? ORDER BY created_at,rowid", (job,))}

    def chat_workspace(project: str, thread: str):
        import cowork, sandbox
        if not ws.project_exists(project):
            raise HTTPException(404, "Project not found")
        return sandbox.Workspace(cowork.tier_for({"project": project}), thread)

    @app.get("/api/workspace/files")
    def workspace_files(project: str, thread: str):
        space = chat_workspace(project, thread)
        if not space.dir.is_dir():
            return {"folder": str(space.dir), "files": []}
        files = []
        for path in sorted(space.dir.rglob("*")):
            if any(part in {".git", "node_modules", "__pycache__", ".venv"} for part in path.relative_to(space.dir).parts):
                continue
            if path.is_file() and not path.is_symlink():
                files.append({"path": str(path.relative_to(space.dir)), "bytes": path.stat().st_size})
            if len(files) >= 500:
                break
        return {"folder": str(space.dir), "files": files}

    @app.get("/api/workspace/file")
    def workspace_file(project: str, thread: str, path: str):
        target = chat_workspace(project, thread).resolve(path)
        if not target.is_file():
            raise HTTPException(404, "File not found")
        return FileResponse(target, filename=target.name, media_type="application/octet-stream")

    @app.get("/api/memories")
    def all_memories(project: str = "all", search: str = ""):
        where, params = ("", ()) if project == "all" else ("AND n.project=?", (project,))
        return {"memories": ws.query(f"""SELECT n.*, p.name AS project_name FROM notes n JOIN projects p ON p.id=n.project
            WHERE (n.title LIKE ? OR n.content LIKE ?) {where} ORDER BY n.updated_at DESC, n.id DESC LIMIT 300""",
            (f"%{search}%", f"%{search}%", *params))}

    @app.post("/api/memories/{note}/delete")
    def delete_memory(note: int):
        with ws.connection() as db, db:
            changed = db.execute("DELETE FROM notes WHERE id=?", (note,)).rowcount
        return {"deleted": bool(changed)}

    @app.post("/api/projects", status_code=201)
    def new_project(body: ProjectIn):
        return {"id": ws.create_project(body.name, body.brief)}

    @app.post("/api/jobs", status_code=201)
    def new_job(body: JobIn):
        # For Cowork, allow_frontier means "may hand work to Claude Code / Codex" (checked when it runs).
        if body.allow_frontier and not hub.FRONTIER_MODEL and body.skill != "cowork":
            raise ValueError("Configure FRONTIER_MODEL before enabling paid advice")
        return {"id": ws.create_job(**body.model_dump())}

    @app.get("/api/jobs/{job}")
    def job_detail(job: str):
        rows = ws.query("SELECT * FROM jobs WHERE id=?", (job,))
        if not rows:
            raise HTTPException(404, "Task not found")
        return {**rows[0], "plan": coordination.plan(job), "events": ws.query("SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT 100", (job,))}

    @app.get("/api/jobs/{job}/events")
    def job_events(job: str, after: int = 0):
        rows = ws.query("SELECT status,result,error FROM jobs WHERE id=?", (job,))
        if not rows:
            raise HTTPException(404, "Task not found")
        return {**rows[0], "plan": coordination.plan(job),
                "events": ws.query("SELECT id,kind,detail,created_at FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT 200", (job, after)),
                "artifacts": ws.query("SELECT id,name,media_type FROM artifacts WHERE job_id=? ORDER BY created_at,rowid", (job,))}

    @app.post("/api/workspace/upload", status_code=201)
    def upload(body: UploadIn):
        """A file the user attached in a Cowork chat, saved into that chat's workspace uploads/ folder."""
        import base64, binascii, cowork, sandbox
        if not ws.project_exists(body.project):
            raise HTTPException(404, "Project not found")
        try:
            content = base64.b64decode(body.content_b64, validate=True)
        except binascii.Error:
            raise ValueError("content_b64 is not valid base64") from None
        space = sandbox.Workspace(cowork.tier_for({"project": body.project}), body.thread).prepare()
        name = Path(body.name).name.replace("\x00", "")[:200] or "upload"
        target = space.write_bytes(f"uploads/{name}", content)
        return {"path": space.relative(target), "bytes": len(content)}

    @app.get("/api/connections")
    def connections():
        import escalate
        return escalate.status()

    @app.post("/api/connections/claude")
    def connect_claude(body: TokenIn):
        import escalate
        escalate.save_claude_token(body.token)
        return escalate.status()["claude"]

    @app.post("/api/admin/code")
    async def stage_code(body: CodeIn):
        """Stage a GitHub commit of the hub's app code; the next instance restart runs it (see supervise.mjs)."""
        import code_update
        return await code_update.stage(body.ref)

    @app.post("/api/connections/codex")
    async def connect_codex():
        import escalate
        return await escalate.codex_login()

    @app.post("/api/jobs/{job}/review")
    def review(job: str):
        coordination.review(job)
        return {"reviewed": True}

    @app.post("/api/jobs/{job}/handoff")
    def handoff(job: str):
        return coordination.handoff(job)

    @app.post("/api/jobs/{job}/cancel")
    def cancel(job: str):
        return {"changed": ws.cancel_job(job)}

    @app.post("/api/jobs/{job}/resume")
    def resume(job: str):
        return {"changed": ws.resume_job(job)}

    @app.post("/api/documents", status_code=201)
    def document(body: DocumentIn):
        return ws.ingest(**body.model_dump())

    @app.get("/api/search")
    def search(project: str, query: str):
        return {"items": ws.search(project, query)}

    @app.post("/api/memories", status_code=201)
    def memory(body: NoteIn):
        return {"id": ws.save_note(**body.model_dump())}

    @app.get("/api/artifacts/{identity}")
    def artifact(identity: str):
        rows = ws.query("SELECT * FROM artifacts WHERE id=?", (identity,))
        if not rows:
            raise HTTPException(404, "Artifact not found")
        row = rows[0]
        path = Path(row["path"]).resolve()
        if not path.is_relative_to((store.DATA / "artifacts").resolve()) or not path.is_file():
            raise HTTPException(404, "Artifact is unavailable")
        return FileResponse(path, filename=row["name"], media_type="application/octet-stream")

    return app


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--no-worker", action="store_true", help="Inspect data without executing queued tasks")
    args = ap.parse_args()
    import uvicorn
    app = create_app(run_worker=not args.no_worker)
    print(f"Open http://127.0.0.1:{args.port}. Owner key is in {store.DATA / 'console.key'} (or CONSOLE_KEY).")
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
