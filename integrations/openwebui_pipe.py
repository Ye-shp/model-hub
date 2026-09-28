"""
title: Model Hub team
description: Run durable, project-scoped Model Hub tasks from Open WebUI.
author: Model Hub
version: 1.0.0
"""
import asyncio
import json
import re
import time
from urllib.parse import urlparse

try:
    import httpx2 as httpx
except ImportError:
    import httpx
from pydantic import BaseModel, Field

SKILLS = {
    "research-brief": "Research team", "content-studio": "Content studio",
    "trend-report": "Trend analyst", "project-planner": "Project planner",
    "code-review": "Code reviewer", "decision-brief": "Decision brief",
    "visual-production": "Visual production",
}
HELP = """Choose a Model Hub team from the model selector and send a self-contained task.
The team uses the configured project's knowledge and memory. Paste important chat context into your brief.

- `/hub tasks` — recent jobs and items needing attention
- `/hub status JOB_ID` — result, progress plan and recent activity
- `/hub cancel JOB_ID` — stop queued/running work
- `/hub resume JOB_ID` — resume saved progress after interruption or cancellation
- `/hub handoff JOB_ID` — export a portable Markdown handoff
- `/hub review JOB_ID` — mark a completed result reviewed

Tasks survive closing this chat. Frontier advice and images are disabled unless the administrator enables them.
Uploaded chat files are not automatically imported into the team's project knowledge.
"""


class Pipe:
    class Valves(BaseModel):
        CONTROLLER_URL: str = Field(default="http://127.0.0.1:8787", description="Controller reachable from the Open WebUI server")
        OWNER_KEY: str = Field(default="", description="Private controller owner key; administrators only")
        PROJECT_ID: str = Field(default="friends", description="Dedicated project for this Pipe (never 'default', which holds the owner's own work). All users of this Pipe share it.")
        PROFILE: str = Field(default="balanced", description="fast, balanced or deep")
        ALLOWED_EMAILS: str = Field(default="", description="Comma-separated invited users; empty means admins only")
        ALLOW_FRONTIER: bool = False
        ALLOW_IMAGES: bool = False
        WAIT_SECONDS: int = Field(default=60, ge=0, le=120, description="Wait in chat, then leave the durable job running")

    def __init__(self):
        self.valves = self.Valves()

    def pipes(self):
        return [{"id": key, "name": "Hub · " + name} for key, name in SKILLS.items()]

    def _client(self):
        return httpx.AsyncClient(base_url=self.valves.CONTROLLER_URL.rstrip("/"),
                                 headers={"Authorization": "Bearer " + self.valves.OWNER_KEY}, timeout=15)

    async def _request(self, client, method, path, **kwargs):
        response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            # Never echo upstream headers, credentials or arbitrary error bodies into chat.
            raise ValueError(f"Controller returned HTTP {response.status_code}. Check the task ID, settings and controller log.")
        return response.json()

    async def _job(self, client, identity):
        if not re.fullmatch(r"[a-f0-9]{32}", identity):
            raise ValueError("Use the full 32-character task ID")
        job = await self._request(client, "GET", f"/api/jobs/{identity}")
        if job["project"] != self.valves.PROJECT_ID:
            raise ValueError("That task is not in this Pipe's project")
        return job

    @staticmethod
    def _status(job):
        lines = [f"**{job['status'].title()}** · Task `{job['id']}`"]
        lines += [f"- {step['status']}: {step['title']}" for step in job.get("plan", [])]
        if job.get("result"):
            result = job["result"]
            lines.append(result[:20000])
            if len(result) > 20000:
                lines.append("Result abbreviated in chat; the full deliverable is saved in the workspace.")
        elif job.get("error"):
            lines.append(job["error"])
        elif job.get("events"):
            lines.append("Latest activity: " + job["events"][0]["detail"])
        return "\n\n".join(lines)

    async def pipe(self, body: dict, __user__: dict = None, __task__: str = None):
        # Open WebUI can call the model for titles, tags and follow-up suggestions.
        # These must never start jobs or incur model/provider calls.
        if __task__:
            yield "Model Hub task"
            return
        if self.valves.PROJECT_ID.strip() in ("", "default"):
            yield "Set this Pipe's PROJECT_ID valve to a dedicated project name first (not 'default')."
            return
        user = __user__ or {}
        allowed = {email.strip().lower() for email in self.valves.ALLOWED_EMAILS.split(",") if email.strip()}
        if user.get("role") != "admin" and (not user.get("email") or user["email"].lower() not in allowed):
            yield "This Model Hub team is available to administrators and explicitly invited users."
            return
        messages = [m for m in body.get("messages", []) if m.get("role") == "user"]
        content = messages[-1].get("content", "") if messages else ""
        if isinstance(content, list):
            if any(part.get("type") != "text" for part in content):
                yield "This team Pipe accepts text briefs. Use your direct vision model for images, then add its observations to the project."
                return
            content = "\n".join(part.get("text", "") for part in content)
        text = content.strip() if isinstance(content, str) else ""
        if not text or text == "/hub help":
            yield HELP
            return
        parsed = urlparse(self.valves.CONTROLLER_URL)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            yield "Configure a valid controller URL in the Pipe's Valves."
            return
        if len(self.valves.OWNER_KEY) < 32 or self.valves.PROFILE not in {"fast", "balanced", "deep"}:
            yield "Configure the private controller owner key and a valid profile in the Pipe's Valves."
            return
        try:
            async with self._client() as client:
                if text.startswith("/hub "):
                    parts = text.split()
                    command = parts[1]
                    if command == "tasks" and len(parts) == 2:
                        state = await self._request(client, "GET", "/api/state", params={"project": self.valves.PROJECT_ID})
                        lines = ["**Needs attention**"] + [f"- `{j['id']}`: {j['reason']}" for j in state.get("attention", [])[:10]]
                        lines += ["**Recent tasks**"] + [f"- `{j['id']}` · {j['status']} · {j['task'][:140]}" for j in state["jobs"][:10]]
                        yield "\n".join(lines)
                        return
                    if command not in {"status", "cancel", "resume", "handoff", "review"} or len(parts) != 3:
                        yield HELP
                        return
                    job = await self._job(client, parts[2])
                    if command == "status":
                        yield self._status(job)
                        return
                    result = await self._request(client, "POST", f"/api/jobs/{job['id']}/{command}")
                    if command == "handoff":
                        markdown = result["markdown"]
                        yield markdown[:20000] + ("\n\nChat excerpt limited to 20,000 characters. Full handoff saved as a workspace deliverable." if len(markdown) > 20000 else "")
                    else:
                        yield f"Task `{job['id']}`: {command} " + ("requested." if result.get("changed", result.get("reviewed")) else "made no change in its current state.")
                    return
                skill = body.get("model", "").rsplit(".", 1)[-1]
                if skill not in SKILLS:
                    yield "Select one of the Hub teams from the model selector."
                    return
                if len(text) > 16000:
                    yield "Keep the task brief under 16,000 characters. Import larger sources into project knowledge."
                    return
                result = await self._request(client, "POST", "/api/jobs", json={
                    "project": self.valves.PROJECT_ID, "task": text, "skill": skill, "profile": self.valves.PROFILE,
                    "allow_frontier": self.valves.ALLOW_FRONTIER, "allow_images": self.valves.ALLOW_IMAGES})
                identity = result["id"]
                yield f"Started task `{identity}`. It keeps running if you close this chat.\n\n"
                deadline = time.monotonic() + self.valves.WAIT_SECONDS
                while time.monotonic() < deadline:
                    job = await self._job(client, identity)
                    if job["status"] not in {"queued", "running"}:
                        yield self._status(job)
                        return
                    # Empty OpenAI delta keeps the HTTP stream alive without filling the transcript.
                    if body.get("stream", False):
                        yield 'data: ' + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": None}]}) + '\n\n'
                    await asyncio.sleep(min(3, max(0, deadline - time.monotonic())))
                yield f"Working in the background. Send `/hub status {identity}` to check progress or `/hub cancel {identity}` to stop it."
        except (httpx.HTTPError, ValueError, KeyError):
            yield "Could not complete the controller request. Check its address, owner key and project settings. " \
                  "If this happened while starting a task, use `/hub tasks` before submitting again: the task may already exist."
