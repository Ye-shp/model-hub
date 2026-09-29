"""Owner's local dashboard and job controller. python agents/console.py --port 8787"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager, suppress
import hmac
import os
from pathlib import Path
import secrets
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import hub  # Reads agents/.env before the data directory is selected.
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
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + token).encode()):
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
