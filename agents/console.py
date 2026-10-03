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
from typing import Literal
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, StrictFloat, StrictInt

import hub  # Reads agents/.env before the data directory is selected.
import access as cf_access
import store
import workspace as ws
import coordination
import audience
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


class SocialIn(BaseModel):
    service: str = Field(min_length=1, max_length=40)
    words: list[str] = Field(default_factory=list, max_length=8)


class TelegramIn(BaseModel):
    token: str = Field(min_length=3, max_length=120)  # a @BotFather token, or "off"


class CodeIn(BaseModel):
    ref: str = Field(pattern=r"^[0-9a-f]{40}$")


class UploadIn(BaseModel):
    project: str
    thread: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=200)
    content_b64: str
    requested_by: str | None = Field(default=None, max_length=200)


class MemoryEdit(BaseModel):
    title: str = Field(min_length=1, max_length=160)
    content: str = Field(max_length=12000)
    kind: str = "fact"


class ChatRef(BaseModel):
    account: str = Field(min_length=1, max_length=40)
    thread: str = Field(min_length=1, max_length=80)


class MigrateIn(BaseModel):
    target: str = Field(pattern=r"^http://[0-9.]{7,15}:[0-9]{2,5}$")
    key: str = Field(min_length=32, max_length=200)


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


class AudienceExperimentIn(BaseModel):
    project: str = "default"
    name: str = Field(min_length=1, max_length=160)
    brief: str = Field(min_length=1, max_length=12000)
    hypothesis: str = Field(default="", max_length=2000)
    platform: Literal["tiktok", "instagram"] = "tiktok"
    account: str = Field(default="", max_length=200)
    kind: str = Field(default="reel", max_length=80)
    context: str = Field(default="", max_length=1000)
    split: Literal["train", "eval"] = "train"


class AudienceVariantIn(BaseModel):
    project: str = "default"
    experiment_id: StrictInt
    label: str = Field(min_length=1, max_length=160)
    response: str = Field(min_length=1, max_length=50000)
    post_id: StrictInt | None = None
    model: str = Field(default="", max_length=300)
    strategy: str = Field(default="", max_length=2000)
    media_hashes: list[str] = Field(default_factory=list, max_length=10)
    job_id: str = Field(default="", max_length=120)


class AudiencePublicationIn(BaseModel):
    project: str = "default"
    remote_id: str = Field(min_length=1, max_length=40)
    url: str = Field(min_length=1, max_length=2000)
    published_at: str = Field(min_length=1, max_length=80)
    account: str = Field(min_length=1, max_length=200)
    exposure: Literal["organic", "paid", "mixed", "unknown"] = "organic"


class AudienceMetricsIn(BaseModel):
    project: str = "default"
    metrics: dict[str, StrictInt | StrictFloat | None]
    observed_at: str | None = Field(default=None, max_length=80)


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


def resolve_project(project: str, requested_by: str | None, thread: str | None = None) -> str:
    """Invited friends each get their own project and sandbox account. The chat site sends project "friends"
    and the friend's email; chats a friend started in the old shared folder move to their own account."""
    if project != "friends" or not requested_by or "@" not in requested_by:
        return project
    import sandbox
    friend = ws.ensure_friend_project(requested_by)
    if thread:
        old = sandbox.ROOT / "guest" / "threads" / sandbox.clean_thread(thread)
        new = sandbox.ROOT / friend / "threads" / sandbox.clean_thread(thread)
        if old.is_dir() and not new.exists():
            space = sandbox.Workspace(friend, thread).prepare()
            space.dir.rmdir()
            old.rename(space.dir)
            if sandbox.IS_ROOT:
                for folder, dirs, files in os.walk(space.dir):
                    for name in [folder, *(os.path.join(folder, n) for n in dirs + files)]:
                        os.chown(name, space.uid, space.gid, follow_symlinks=False)
    return friend


def create_app(key: str | None = None, run_worker: bool = True, runner=None) -> FastAPI:
    ws.init()
    audience.init()
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
        import toolbox
        toolbox.install_in_background()  # free research/video/social tools, once (kept across restarts)
        import telegram_bot
        import audience_worker
        with controller_lock():
            ws.recover_jobs()
            task = asyncio.create_task(serve(runner or run_job, lambda: bool(hub.HUB_URL and hub.HUB_KEY)))
            telegram = asyncio.create_task(telegram_bot.serve())  # idle until /connect telegram
            metrics = asyncio.create_task(audience_worker.serve())  # durable checkpoints, independent of chat timeouts
            try:
                yield
            finally:
                for running in (task, telegram, metrics):
                    running.cancel()
                    with suppress(asyncio.CancelledError):
                        await running

    app = FastAPI(title="Model Hub workspace", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def access(request: Request, call_next):
        host = urlparse("http://" + request.headers.get("host", "")).hostname
        if host not in allowed_hosts:
            return JSONResponse({"error": "Unexpected hostname"}, status_code=403)
        if request.url.path.startswith("/bridge/"):
            import phone_link
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied.encode(), ("Bearer " + phone_link.key()).encode()):
                return JSONResponse({"error": "Invalid bridge key"}, status_code=401)
            try:
                if int(request.headers.get("content-length", "0")) > 40_000_000:
                    return JSONResponse({"error": "Too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "Invalid content length"}, status_code=400)
        elif request.url.path.startswith("/api/"):
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

    @app.get("/bridge.py")
    def bridge_script():
        """The phone bridge program for the owner's PC (no secrets in it; the key is entered when it's started)."""
        return FileResponse(WEB / "bridge.py", filename="bridge.py", media_type="text/x-python")

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
        # Chat-site tasks carry the conversation first; show the request itself.
        return {"jobs": ws.query(f"""SELECT j.id, j.project, p.name AS project_name, j.skill, j.profile, j.status,
            CASE WHEN instr(j.task, 'CURRENT REQUEST:') > 0 THEN substr(j.task, instr(j.task, 'CURRENT REQUEST:') + 17, 400)
                 ELSE substr(j.task, 1, 400) END AS task,
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
        data = body.model_dump()
        data["project"] = resolve_project(body.project, body.requested_by, body.thread)
        return {"id": ws.create_job(**data)}

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
        following = ws.query("SELECT id FROM jobs WHERE parent=? ORDER BY created_at LIMIT 1", (job,))
        return {**rows[0], "plan": coordination.plan(job), "next_job": following[0]["id"] if following else None,
                "events": ws.query("SELECT id,kind,detail,created_at FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT 200", (job, after)),
                "artifacts": ws.query("SELECT id,name,media_type FROM artifacts WHERE job_id=? ORDER BY created_at,rowid", (job,))}

    @app.get("/api/thread/active")
    def thread_active(project: str, thread: str, requested_by: str | None = None):
        """The unfinished task of a chat (running or queued), so the chat site can follow it again."""
        project = resolve_project(project, requested_by)
        rows = ws.query("""SELECT id,status,created_at FROM jobs WHERE project=? AND thread=? AND status IN ('running','queued')
                           ORDER BY (status='running') DESC, created_at LIMIT 1""", (project, thread))
        return {"job": rows[0] if rows else None}

    @app.post("/api/workspace/upload", status_code=201)
    def upload(body: UploadIn):
        """A file the user attached in a Cowork chat, saved into that chat's workspace uploads/ folder."""
        import base64, binascii, cowork, sandbox
        project = resolve_project(body.project, body.requested_by, body.thread)
        if not ws.project_exists(project):
            raise HTTPException(404, "Project not found")
        try:
            content = base64.b64decode(body.content_b64, validate=True)
        except binascii.Error:
            raise ValueError("content_b64 is not valid base64") from None
        space = sandbox.Workspace(cowork.tier_for({"project": project}), body.thread).prepare()
        name = Path(body.name).name.replace("\x00", "")[:200] or "upload"
        target = space.write_bytes(f"uploads/{name}", content)
        return {"path": space.relative(target), "bytes": len(content)}

    @app.get("/api/connections")
    def connections():
        import escalate
        return escalate.status()

    @app.get("/api/connections/social")
    def social_connections():
        import toolbox
        return {"accounts": toolbox.connected(), "tools": toolbox.status(), "help": toolbox.HELP}

    @app.post("/api/connections/social")
    def connect_social(body: SocialIn):
        import toolbox
        service = body.service.lower()
        if body.words == ["off"]:
            return {"accounts": toolbox.forget(service)}
        return {"accounts": toolbox.save_credentials(service, toolbox.parse_connect(service, body.words))}

    @app.get("/api/connections/telegram")
    def telegram_status():
        import telegram_bot
        return telegram_bot.status()

    @app.post("/api/connections/telegram")
    async def connect_telegram(body: TelegramIn):
        import telegram_bot
        if body.token.strip().lower() == "off":
            return telegram_bot.disconnect()
        try:
            return await telegram_bot.connect(body.token)
        except RuntimeError as error:  # Telegram refused the token
            raise ValueError(str(error)) from None

    @app.get("/api/posts")
    def social_posts(project: str = "default"):
        import research_tools
        research_tools.init()
        if not ws.project_exists(project):
            raise HTTPException(404, "Project not found")
        return {"posts": ws.query("SELECT id,platform,kind,status,caption,result,created_at,updated_at FROM social_posts "
                                  "WHERE project=? ORDER BY id DESC LIMIT 50", (project,))}

    # Audience experiments never publish posts or start training. Every record belongs to one project.
    @app.get("/api/audience/experiments")
    def audience_experiments(project: str = "default"):
        return {"experiments": audience.list_experiments(project)}

    @app.post("/api/audience/experiments", status_code=201)
    def audience_create_experiment(body: AudienceExperimentIn):
        return audience.create_experiment(**body.model_dump())

    @app.get("/api/audience/experiments/{experiment_id}")
    def audience_experiment(experiment_id: int, project: str = "default"):
        return audience.experiment_detail(project, experiment_id)

    @app.post("/api/audience/variants", status_code=201)
    def audience_create_variant(body: AudienceVariantIn):
        return audience.register_variant(**body.model_dump())

    @app.post("/api/audience/variants/{variant_id}/publication")
    def audience_publication(variant_id: int, body: AudiencePublicationIn):
        return audience.confirm_publication(variant_id=variant_id, **body.model_dump())

    @app.post("/api/audience/variants/{variant_id}/metrics", status_code=201)
    def audience_metrics(variant_id: int, body: AudienceMetricsIn):
        # Clients cannot impersonate an automated platform collector.
        return audience.record_snapshot(variant_id=variant_id, source="manual", **body.model_dump())

    @app.get("/api/audience/performance")
    def audience_performance(project: str = "default"):
        return audience.performance_summary(project)

    @app.get("/api/audience/preferences")
    def audience_preferences(project: str = "default", horizon_hours: int = 72, min_views: int = 500,
                             min_margin: float = 0.15):
        return audience.export_preferences(project, horizon_hours, min_views, min_margin)

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

    # ---- phone bridge (the PC program talks to these with the bridge key) ----
    @app.post("/bridge/poll")
    async def bridge_poll(request: Request):
        import phone_link
        info = await request.json()
        return {"commands": await phone_link.poll(info if isinstance(info, dict) else {})}

    @app.post("/bridge/result")
    async def bridge_result(request: Request):
        import phone_link
        result = await request.json()
        return {"accepted": phone_link.deliver(result if isinstance(result, dict) else {})}

    @app.get("/bridge/file/{token}")
    def bridge_file(token: str):
        import phone_link
        path = phone_link.FILES.get(token)
        if not path:
            raise HTTPException(404, "Unknown or expired file")
        return FileResponse(path, filename=Path(path).name, media_type="application/octet-stream")

    @app.get("/api/phone")
    def phone_state():
        import phone_link
        screen = dict(phone_link.BRIDGE.last_screen)
        return {**phone_link.status(), "key": phone_link.key(), "bridge_url": os.environ.get("BRIDGE_PUBLIC_URL", ""),
                "screen": screen or None,
                "posts": ws.query("SELECT platform, COUNT(*) AS n, MAX(collected_at) AS last FROM posts GROUP BY platform")}

    @app.post("/api/phone/key")
    def phone_rotate():
        import phone_link
        return {"key": phone_link.rotate_key()}

    @app.post("/api/phone/screen")
    async def phone_screen_now():
        import phone_link
        try:
            await phone_link.screen(client=None, describe=False)
        except RuntimeError as error:
            raise ValueError(str(error)) from None
        return {"screen": phone_link.BRIDGE.last_screen}

    # ---- health: GPUs, models, disk, image box, code version ----
    @app.get("/api/health")
    async def health():
        import sandbox
        return await asyncio.to_thread(system_health, sandbox)

    # ---- chats: every chat folder, its size and tasks; delete to free disk ----
    @app.get("/api/chats")
    def chats():
        import sandbox
        found = []
        names = {r["id"]: r["name"] for r in ws.query("SELECT id,name FROM projects")}
        if sandbox.ROOT.is_dir():
            for base in sorted(sandbox.ROOT.iterdir()):
                if not base.is_dir() or not sandbox.is_account(base.name) or not (base / "threads").is_dir():
                    continue
                for folder in (base / "threads").iterdir():
                    if not folder.is_dir() or folder.name == "connections":  # Claude/Codex sign-in scratch space
                        continue
                    jobs = ws.query("""SELECT id,status,requested_by,substr(task,1,20000) AS task,created_at FROM jobs WHERE thread=?
                                       ORDER BY created_at DESC LIMIT 1""", (folder.name,))
                    count = ws.query("SELECT COUNT(*) AS n FROM jobs WHERE thread=?", (folder.name,))[0]["n"]
                    last = jobs[0] if jobs else {}
                    request = (last.get("task") or "").split("CURRENT REQUEST:\n", 1)[-1].strip()[:200]
                    found.append({"account": base.name, "owner": names.get(base.name, "owner" if base.name == "owner" else base.name),
                                  "thread": folder.name, "bytes": sandbox.folder_bytes(folder, max_age=300),
                                  "modified": folder.stat().st_mtime, "tasks": count, "last_status": last.get("status"),
                                  "last_request": request, "who": last.get("requested_by")})
        found.sort(key=lambda c: c["modified"], reverse=True)
        return {"chats": found, "free_gb": round(sandbox.free_gb(), 1)}

    @app.post("/api/chats/delete")
    def delete_chat(body: ChatRef):
        import shutil as sh
        import sandbox
        if not sandbox.is_account(body.account):
            raise ValueError("Unknown account")
        folder = sandbox.ROOT / body.account / "threads" / sandbox.clean_thread(body.thread)
        if ws.query("SELECT 1 FROM jobs WHERE thread=? AND status IN ('running','queued') LIMIT 1", (folder.name,)):
            raise ValueError("A task is still running in this chat; stop it first")
        if not folder.is_dir():
            raise HTTPException(404, "Chat folder not found")
        sh.rmtree(folder)
        sandbox._sizes.clear()
        return {"deleted": True, "free_gb": round(sandbox.free_gb(), 1)}

    @app.post("/api/memories/{note}")
    def edit_memory(note: int, body: MemoryEdit):
        if body.kind not in {"fact", "decision", "preference", "checkpoint", "question"}:
            raise ValueError("Unknown memory kind")
        with ws.connection() as db, db:
            changed = db.execute("UPDATE notes SET title=?,content=?,kind=?,updated_at=? WHERE id=?",
                                 (body.title, body.content, body.kind, store.now(), note)).rowcount
        if not changed:
            raise HTTPException(404, "Memory not found")
        return {"updated": True}

    # ---- moving everything to a new server (see agents/migrate.py) ----
    @app.post("/api/admin/migrate")
    async def migrate_out(body: MigrateIn):
        import migrate
        if ws.query("SELECT 1 FROM jobs WHERE status='running' LIMIT 1"):
            raise ValueError("A task is running; wait for it to finish (or stop it) before moving the data")
        return migrate.start_send(body.target, body.key)

    @app.get("/api/admin/migrate")
    def migrate_status():
        import migrate
        return migrate.SEND

    return app


def system_health(sandbox) -> dict:
    """What the console's Now page shows about the box. Every part is best-effort."""
    import shutil as sh
    import subprocess
    import urllib.request
    report: dict = {"at": store.now()}
    disks = {}
    for label, path in (("data", store.DATA), ("chats", sandbox.ROOT), ("models", Path(os.environ.get("MODEL_DIR", "/workspace/models")))):
        target = path
        while not target.exists() and target != target.parent:
            target = target.parent
        usage = sh.disk_usage(target)
        disks[label] = {"path": str(target), "total_gb": round(usage.total / 1024**3, 1), "free_gb": round(usage.free / 1024**3, 1),
                        "used_pct": round(100 * usage.used / usage.total)}
    report["disks"] = disks
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10).stdout
        report["gpus"] = [dict(zip(("index", "name", "util", "mem_used", "mem_total", "temp"), [v.strip() for v in line.split(",")]))
                          for line in out.strip().splitlines() if line.strip()]
    except (OSError, subprocess.SubprocessError):
        report["gpus"] = []
    try:
        req = urllib.request.Request(hub.HUB_URL + "/status", headers={"Authorization": "Bearer " + hub.HUB_KEY})
        with urllib.request.urlopen(req, timeout=5) as response:
            report["models"] = json.loads(response.read())["models"]
    except Exception as error:
        report["models"] = {"error": type(error).__name__}
    try:
        report["startup"] = json.loads((store.DATA.parent / "status.json").read_text())
    except (OSError, ValueError):
        report["startup"] = None
    image = os.environ.get("MODEL3_URL", "")
    if image:
        try:
            with urllib.request.urlopen(image.rsplit("/v1", 1)[0] + "/health", timeout=6) as response:
                report["image_box"] = {"ok": response.status == 200}
        except Exception as error:
            report["image_box"] = {"ok": False, "error": type(error).__name__}
    code = os.environ.get("HUB_CODE_DIR", "")
    try:
        report["code"] = (Path(code) / "active").read_text().strip() if code else None
    except OSError:
        report["code"] = None
    report["jobs"] = ws.query("SELECT status, COUNT(*) AS n FROM jobs WHERE status IN ('running','queued') GROUP BY status")
    return report


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
