"""
title: Qwen Cowork
description: Type what you want done. Qwen plans it and uses its workspace, web, helpers and connected services. Owners can sign in to Higgsfield with /connect higgsfield.
author: Model Hub
version: 1.0.0
"""
import asyncio
import base64
import io
import json
import re
import time
from urllib.parse import urlparse

try:
    import httpx2 as httpx
except ImportError:
    import httpx
from pydantic import BaseModel, Field

HELP = """**Qwen Cowork** — say what you want accomplished; it plans, works with its tools and hands back results and files.

Each chat has its own workspace folder that persists, so follow-ups build on earlier files. Attach files and they land in `uploads/`.
Pressing stop cancels the task. Send `status` to follow a task that's still running (for example after the page reloaded).
Big projects: ask for a plan first; Cowork keeps `plan.md` in the chat's folder and can run the next phase automatically.

Owner commands: `/connections` (status of everything) · `/connect claude TOKEN` (token from `claude setup-token`) ·
`/connect codex` · `/connect x USERNAME auth_token=… ct0=…` · `/connect instagram USER_ID ACCESS_TOKEN` ·
`/connect tiktok OPEN_ID ACCESS_TOKEN` (add `refresh_token=… client_key=… client_secret=…` for automatic renewal) ·
`/connect bluesky HANDLE APP_PASSWORD` · `/connect github TOKEN` · `/connect telegram BOT_TOKEN` (use Cowork from
Telegram) · `/connect higgsfield` (sign in with your existing account; `off` disconnects) ·
`/connect typesafe API_KEY` (Jev, TypeSafe's judgment model; `off` disconnects) ·
`/connect mcp NAME URL [bearer=KEY]` or `/connect mcp NAME stdio COMMAND… [env:KEY=value]` (MCP servers) ·
`/connect api NAME BASE_URL [bearer=KEY] [header="Name: value"] [about="…"]` (HTTP APIs). Approve a drafted post with `approve post N`."""

SKIP = {"model", "usage", "queued", "completed", "failed", "interrupted", "cancelled", "resumed", "frontier-call", "partial",
        "next-phase"}


def question_text(question: dict) -> str:
    """A question from a running task (asking.py on the controller), as the chat shows it."""
    lines = ["❓ **Cowork has a question**", "", str(question.get("question") or "")]
    options = question.get("options") or []
    if options:
        lines += [""] + [f"{n}. {option}" for n, option in enumerate(options, 1)] + ["", "Reply with a number or your own answer."]
    else:
        lines += ["", "Reply in this chat to answer."]
    lines.append(f"_The task is paused and waits up to {question.get('wait_minutes') or 30} minutes; after that it "
                 "continues with its best assumption._")
    return "\n".join(lines)


class LostContact(Exception):
    pass


def connection_error(error, request_text: str) -> str:
    """Controller validation errors must not echo credentials pasted into a connection command."""
    text = str(error)
    values = set()
    for word in request_text.split()[2:]:
        values.add(word)
        if "=" in word:
            values.add(word.partition("=")[2])
    for value in sorted((v for v in values if v), key=len, reverse=True):
        text = text.replace(value, "[redacted]")
    return text


def higgsfield_tools(state: dict) -> list[str]:
    tools = state.get("tools")
    return [name for name in tools if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name)] \
        if isinstance(tools, list) else []


def higgsfield_connected(state: dict) -> str:
    tools = higgsfield_tools(state)
    summary = (f": {len(tools)} tools ({', '.join(name.split('__', 1)[-1] for name in tools[:12])}"
               f"{'…' if len(tools) > 12 else ''})") if tools else ""
    return f"**Higgsfield** connected{summary}. Cowork uses its tools from your next task."


def hide_higgsfield_auth(text: str) -> str:
    """Sign-in links remain clickable in chat but never enter later task prompts."""
    pattern = r"https://clerk\.higgsfield\.ai/oauth/authorize(?:\?[^\s<>)]*)?"
    pattern += r"|https?://[^\s<>)]*/api/connections/higgsfield/callback\?[^\s<>)]*"
    return re.sub(pattern, "(Higgsfield sign-in details, hidden)", text, flags=re.IGNORECASE)


CHAT_HELP = """**Qwen (chat)** — talk to Qwen. It answers directly and uses the same tools as Cowork (web, shell and files,
images, helpers, Claude Code) when they make the answer better. Each chat has its own workspace folder.
For long projects with plans and phases, use **Qwen Cowork**. Owner commands (`/connections`, `/connect …`) work here too."""


class Pipe:
    class Valves(BaseModel):
        CONTROLLER_URL: str = Field(default="http://127.0.0.1:8787", description="Agent controller, from the chat site's server")
        OWNER_KEY: str = Field(default="", description="Controller owner key (set by the hub on start)")
        MODE: str = Field(default="cowork", description="cowork (autonomous projects) or chat (conversation, same tools)")
        OWNER_PROFILE: str = Field(default="deep", description="fast (20 min), balanced (60 min) or deep (2 h)")
        ALLOW_IMAGES: bool = False
        OWNER_ESCALATION: bool = Field(default=True, description="Let the owner's tasks hand work to Claude Code / Codex")
        HISTORY_CHARACTERS: int = Field(default=60000, ge=2000, le=110000, description="How much of the chat is sent along")
        RETRY_MINUTES: int = Field(default=15, ge=1, le=120, description="Keep retrying the controller this long before giving up")

    def __init__(self):
        self.valves = self.Valves()
        self.file_handler = True  # the agent gets the raw attached files instead of Open WebUI's retrieval snippets

    # ---- helpers ----
    def _client(self):
        return httpx.AsyncClient(base_url=self.valves.CONTROLLER_URL.rstrip("/"),
                                 headers={"Authorization": "Bearer " + self.valves.OWNER_KEY}, timeout=60)

    async def _call(self, client, method, path, **kwargs):
        response = await client.request(method, path, **kwargs)
        if response.status_code >= 400:
            detail = ""
            try:
                detail = response.json().get("error") or response.json().get("detail") or ""
            except ValueError:
                pass
            raise ValueError(f"Controller HTTP {response.status_code} {str(detail)[:200]}".strip())
        return response.json()

    @staticmethod
    def _tier(user: dict):
        """Only the site's admin (the owner) can use it."""
        return "owner" if user.get("role") == "admin" else None

    @staticmethod
    def _text(content) -> tuple[str, list[str]]:
        """Message text and any inline images (data URLs)."""
        if isinstance(content, str):
            return content, []
        texts, images = [], []
        for part in content or []:
            if part.get("type") == "text":
                texts.append(part.get("text", ""))
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:image/"):
                    images.append(url)
        return "\n".join(texts), images

    @staticmethod
    def _thread(metadata: dict, chat_id) -> str:
        raw = chat_id or (metadata or {}).get("chat_id") or (metadata or {}).get("session_id") or "chat"
        return re.sub(r"[^A-Za-z0-9_-]", "-", str(raw))[:80] or "chat"

    async def _status(self, emit, text: str, done: bool = False):
        if emit:
            await emit({"type": "status", "data": {"description": text[:200], "done": done}})

    async def _upload_inputs(self, client, project, thread, files, images, email: str = "") -> list[str]:
        saved = []
        for index, url in enumerate(images, 1):
            header, data = url.split(",", 1)
            extension = header.split("/")[1].split(";")[0].replace("jpeg", "jpg")[:5]
            result = await self._call(client, "POST", "/api/workspace/upload", json={
                "project": project, "thread": thread, "name": f"pasted-image-{int(time.time())}-{index}.{extension}", "content_b64": data,
                "requested_by": email})
            saved.append(result["path"])
        for item in files or []:
            try:
                from open_webui.models.files import Files
                from open_webui.storage.provider import Storage
                identity = item.get("id") or (item.get("file") or {}).get("id")
                record = await Files.get_file_by_id(identity) if identity else None
                if not record:
                    continue
                with open(Storage.get_file(record.path), "rb") as handle:
                    content = handle.read(60_000_001)
                if len(content) > 60_000_000:
                    saved.append(f"(skipped {record.filename}: larger than 60 MB)")
                    continue
                result = await self._call(client, "POST", "/api/workspace/upload", json={
                    "project": project, "thread": thread, "name": record.filename, "content_b64": base64.b64encode(content).decode(),
                    "requested_by": email})
                saved.append(result["path"])
            except Exception as error:  # one unreadable attachment shouldn't stop the task
                saved.append(f"(could not attach {item.get('name', 'a file')}: {type(error).__name__})")
        return saved

    async def _deliver(self, client, artifacts, user, request, metadata, emit) -> str:
        """Copy the files the agent shared into the chat site so they show up (and download) in the chat."""
        latest = {}
        for artifact in artifacts:  # a file shared twice (e.g. regenerated) is delivered once, newest version
            latest.pop(artifact["name"], None)
            latest[artifact["name"]] = artifact
        artifacts = list(latest.values())
        if not artifacts:
            return ""
        lines, shown = ["", "", "**Files**"], []
        for artifact in artifacts:
            name, media = artifact["name"], artifact.get("media_type") or "application/octet-stream"
            try:
                response = await client.get(f"/api/artifacts/{artifact['id']}")
                response.raise_for_status()
                from fastapi import UploadFile
                from open_webui.models.users import Users
                from open_webui.routers.files import upload_file_handler
                account = await Users.get_user_by_id(user["id"])
                item = await upload_file_handler(request, file=UploadFile(file=io.BytesIO(response.content), filename=name,
                                                                          headers={"content-type": media}),
                                                 metadata={"chat_id": metadata.get("chat_id"), "message_id": metadata.get("message_id")},
                                                 process=False, user=account)
                url = request.app.url_path_for("get_file_content_by_id", id=item.id)
                shown.append({"type": "image" if media.startswith("image/") else "file", "id": item.id, "url": url,
                              "name": name, "content_type": media})
                lines.append(f"- [{name}]({url})")
            except Exception as error:
                lines.append(f"- {name} (saved in the workspace; could not attach here: {type(error).__name__})")
        if emit and shown:
            try:
                await emit({"type": "files", "data": {"files": shown}})
            except Exception:
                pass
        return "\n".join(lines)

    @staticmethod
    def _plan_block(plan) -> str:
        if not plan:
            return ""
        done = sum(1 for s in plan if s["status"] == "completed")
        marks = {"completed": "✅", "in_progress": "▶️", "pending": "⬜"}
        rows = "\n".join(f"{marks.get(s['status'], '⬜')} {s['title']}" for s in plan)
        return f"<details>\n<summary>Task list ({done}/{len(plan)} done)</summary>\n\n{rows}\n</details>\n\n"

    def _describe(self, event, plan) -> str | None:
        kind, detail = event["kind"], event["detail"]
        if kind in SKIP:
            return None
        if kind == "started":
            return "Working…"
        if kind == "plan":
            active = next((s["title"] for s in plan if s["status"] == "in_progress"), None)
            done = sum(1 for s in plan if s["status"] == "completed")
            return f"[{done}/{len(plan)}] {active}" if active else f"Task list: {done}/{len(plan)} done"
        if kind == "artifact":
            try:
                return "Shared " + json.loads(detail)["name"]
            except (ValueError, KeyError):
                return "Shared a file"
        if kind == "escalation":
            agent, _, task = detail.partition(":")
            return f"{'Claude Code' if agent == 'claude' else 'Codex'} is working on:{task}"
        return detail

    async def _poll(self, client, identity: str, after: int, emit) -> dict:
        """One check-in with the controller, retried through restarts and brief outages (up to RETRY_MINUTES)."""
        failures, first = 0, None
        while True:
            try:
                return await self._call(client, "GET", f"/api/jobs/{identity}/events", params={"after": after})
            except (httpx.HTTPError, ValueError) as error:
                if isinstance(error, ValueError) and "HTTP 404" in str(error):
                    raise
                failures += 1
                first = first or time.monotonic()
                if time.monotonic() - first > self.valves.RETRY_MINUTES * 60:
                    raise LostContact(str(error)) from None
                if failures == 2:
                    await self._status(emit, "Reconnecting to the agent controller… (the task keeps running)")
                await asyncio.sleep(min(2 * failures, 15))

    async def _follow(self, client, identity: str, emit, body: dict, context: tuple):
        """Stream a task's progress, then its result; keeps following automatic next phases."""
        shown = 0
        while identity:
            after, plan, last_status, state = 0, [], "", None
            last_keepalive = time.monotonic()
            try:
                while True:
                    try:
                        state = await self._poll(client, identity, after, emit)
                    except LostContact as error:
                        await self._status(emit, "Lost contact with the agent controller", done=True)
                        yield ((f"\n\n---\n\n" if shown else "") +
                               f"I lost contact with the agent controller ({str(error)[:150]}). The task may still be running on "
                               "the box: send **status** in this chat to pick it up again.")
                        return
                    plan = state.get("plan") or plan
                    for event in state["events"]:
                        after = max(after, event["id"])
                        text = self._describe(event, plan)
                        if text and text != last_status:
                            await self._status(emit, text)
                            last_status = text
                    if state["status"] not in {"queued", "running"} and not state["events"]:
                        break
                    if state.get("question") and state["status"] == "running":
                        # The task asked the user something (ask_user). End this reply with the question; the
                        # user's next message in this chat is sent to the task as the answer.
                        await self._status(emit, "Waiting for your answer", done=True)
                        yield ("\n\n---\n\n" if shown else "") + self._plan_block(plan) + question_text(state["question"])
                        return
                    if time.monotonic() - last_keepalive > 15 and body.get("stream"):
                        last_keepalive = time.monotonic()
                        yield "data: " + json.dumps({"choices": [{"index": 0, "delta": {}, "finish_reason": None}]}) + "\n\n"
                    await asyncio.sleep(1.5)
            except asyncio.CancelledError:  # the user pressed stop
                try:
                    async with self._client() as stopper:
                        await stopper.post(f"/api/jobs/{identity}/cancel")
                except Exception:
                    pass
                raise
            user, request, metadata = context
            files = await self._deliver(client, state.get("artifacts") or [], user, request, metadata or {}, emit)
            if state["status"] == "completed":
                text = self._plan_block(plan) + (state.get("result") or "(no reply)") + files
            elif state["status"] == "cancelled":
                text = "Stopped. Files made so far are still in this chat's workspace." + files
            else:
                text = (self._plan_block(plan) + f"The task stopped before finishing: {state.get('error') or state['status']}\n\n"
                        "Everything it made is still in this chat's workspace. Reply **continue** to pick up where it left off."
                        + files)
            identity = state.get("next_job") if state["status"] == "completed" else None
            if identity:
                shown += 1
                text += (f"\n\n---\n\n**Part {shown + 1} started automatically.** It keeps going in this chat; press "
                         "stop to end it.\n\n")
                await self._status(emit, f"Part {shown + 1} starting…")
            yield text
        await self._status(emit, {"completed": "Done", "cancelled": "Stopped"}.get(state["status"], "Stopped early"), done=True)

    # ---- entry point ----
    async def pipe(self, body: dict, __user__: dict = None, __metadata__: dict = None, __files__: list = None,
                   __event_emitter__=None, __task__: str = None, __request__=None, __chat_id__: str = None):
        if __task__:  # titles, tags, follow-ups: never start work for these
            yield "Cowork task"
            return
        user = __user__ or {}
        tier = self._tier(user)
        if not tier:
            yield "Only the owner can use this model."
            return
        if len(self.valves.OWNER_KEY) < 32:
            yield "Qwen Cowork isn't configured yet (missing controller key). Restart the hub or set the Pipe's valves."
            return
        messages = [m for m in body.get("messages", []) if m.get("role") in {"user", "assistant"}]
        chat = self.valves.MODE == "chat"
        if not messages or messages[-1]["role"] != "user":
            yield CHAT_HELP if chat else HELP
            return
        request_text, images = self._text(messages[-1].get("content"))
        request_text = request_text.strip()
        command = request_text.lower()
        project = "default" if tier == "owner" else "friends"
        thread = self._thread(__metadata__, __chat_id__)
        emit = __event_emitter__
        try:
            async with self._client() as client:
                if command in {"/help", "help"} or (not request_text and not images and not __files__):
                    yield CHAT_HELP if chat else HELP
                    return
                if command in {"/connections", "/connect"}:
                    if tier != "owner":
                        yield "Only the owner can manage connections."
                        return
                    info = await self._call(client, "GET", "/api/connections")
                    labels = {"claude": ("Claude Code", "tasks"), "codex": ("Codex", "tasks"), "jev": ("Jev (TypeSafe)", "requests")}
                    rows = [f"- **{labels.get(k, (k, ''))[0]}**: "
                            f"{'connected' if v['signed_in'] else 'not connected'}{'' if v['installed'] else ' (not installed)'}; "
                            f"{v['used_today']}/{v['daily_limit']} {labels.get(k, (k, 'tasks'))[1]} in the last 24 h"
                            for k, v in info.items()]
                    social = await self._call(client, "GET", "/api/connections/social")
                    names = {"x": "X", "instagram": "Instagram (posting)", "tiktok": "TikTok (analytics)", "bluesky": "Bluesky", "github": "GitHub",
                             "scrapecreators": "ScrapeCreators (optional)"}
                    rows += [f"- **{names.get(k, k)}**: {'connected' + (' as @' + v['username'] if v.get('username') else '') if v['connected'] else 'not connected'}"
                             for k, v in social["accounts"].items()]
                    tools = social["tools"]
                    rows.append(f"- **Research tools**: {tools['state']}" + (f" ({tools.get('detail')})" if tools.get("detail") else ""))
                    try:
                        higgsfield = await self._call(client, "GET", "/api/connections/higgsfield")
                        state = "connected" if higgsfield.get("connected") else (
                            "sign-in pending" if higgsfield.get("status") in {"connecting", "awaiting_sign_in"} else "not connected")
                        count = len(higgsfield_tools(higgsfield))
                        rows.append(f"- **Higgsfield**: {state}" + (f" ({count} tools)" if count else "") +
                                    (" (`/connect higgsfield`)" if not higgsfield.get("connected") else ""))
                    except (httpx.HTTPError, ValueError, KeyError):
                        rows.append("- **Higgsfield**: unavailable (`/connect higgsfield`)")
                    try:
                        connected = await self._call(client, "GET", "/api/connections/tools")
                        for name, item in connected["mcp"].items():
                            if name == "higgsfield":  # shown through the native account connection above
                                continue
                            rows.append(f"- **MCP {name}**: {item['transport']} ({item['target']})")
                        for name, item in connected["api"].items():
                            rows.append(f"- **API {name}**: {item['host']} ({', '.join(item['methods'])})")
                    except (httpx.HTTPError, ValueError, KeyError):
                        pass
                    try:
                        telegram = await self._call(client, "GET", "/api/connections/telegram")
                        rows.append("- **Telegram**: " + (("@" + str(telegram["bot"]) + (" (paired)" if telegram["paired"] else
                                                                                        " (waiting for /start <code>)"))
                                                          if telegram["connected"] else "not connected (`/connect telegram <bot token>`)"))
                    except (httpx.HTTPError, ValueError, KeyError):
                        pass
                    yield "\n".join(rows + ["", "To connect Claude Code: run `claude setup-token` on your computer, then send "
                                                 "`/connect claude <token>`. To connect Codex: send `/connect codex`.", ""] +
                                     [f"- {social['help'][k]}" for k in social["help"]] +
                                     ["", "Disconnect an account with `/connect <name> off`."])
                    return
                if command.split()[:2] == ["/connect", "higgsfield"]:
                    if tier != "owner":
                        yield "Only the owner can manage connections."
                        return
                    words = command.split()
                    if len(words) > 3 or (len(words) == 3 and words[2] != "off"):
                        yield "Use `/connect higgsfield` to sign in with your existing account, or `/connect higgsfield off` to disconnect."
                        return
                    action = "disconnect" if len(words) == 3 else "connect"
                    await self._status(emit, "Disconnecting Higgsfield…" if action == "disconnect" else "Checking Higgsfield sign-in…")
                    try:
                        state = await self._call(client, "POST", "/api/connections/higgsfield", json={"action": action}, timeout=180)
                    except (httpx.HTTPError, ValueError):
                        yield "Higgsfield could not be reached. Send `/connect higgsfield` to try again."
                        return
                    if action == "disconnect":
                        yield "Higgsfield disconnected." if not state.get("connected") and not state.get("error") else \
                              "Higgsfield could not disconnect. Try `/connect higgsfield off` again."
                        return
                    if state.get("connected"):
                        yield higgsfield_connected(state)
                        return
                    url = state.get("authorization_url")
                    target = urlparse(url) if isinstance(url, str) else None
                    if target and target.scheme == "https" and target.hostname == "clerk.higgsfield.ai" \
                            and not target.username and not target.password and target.path == "/oauth/authorize" \
                            and len(url) <= 8000 and not any(char in url for char in '\r\n<>"()'):
                        yield (f"[Sign in to Higgsfield]({url}) with your existing account and approve Cowork. "
                               "After sign-in, return here and send `/connect higgsfield` to check the connection. "
                               "Your credentials stay on the hub.")
                    elif state.get("status") in {"connecting", "awaiting_sign_in"} and not state.get("error"):
                        yield "Higgsfield sign-in is still pending. Send `/connect higgsfield` again to check it."
                    else:
                        yield "Higgsfield could not connect. Send `/connect higgsfield` to try again."
                    return
                if command.split()[:2] in (["/connect", "mcp"], ["/connect", "api"]):
                    if tier != "owner":
                        yield "Only the owner can manage connections."
                        return
                    kind = command.split()[1]
                    text = request_text.split(None, 2)[2] if len(request_text.split(None, 2)) == 3 else ""
                    if kind == "mcp" and len(text.split()) >= 2 and text.split()[1].lower() != "off":
                        await self._status(emit, "Connecting to the MCP server and listing its tools…")
                    try:
                        result = await self._call(client, "POST", "/api/connections/tools", json={"kind": kind, "text": text},
                                                  timeout=180)
                    except ValueError as error:
                        yield f"Not saved: {connection_error(error, request_text)}"
                        return
                    if result["removed"]:
                        yield f"**{result['name']}** ({kind.upper()}) disconnected."
                        return
                    if kind == "api":
                        yield (f"API **{result['name']}** saved ({result['host']}, {', '.join(result['methods'])}). Cowork can "
                               "call it with call_api from your next task; the keys stay on the box. You can delete this message.")
                        return
                    check = result.get("check") or {}
                    if check.get("tools"):
                        yield (f"MCP server **{result['name']}** connected: {len(check['tools'])} tools "
                               f"({', '.join(t.split('__', 1)[-1] for t in check['tools'][:12])}"
                               f"{'…' if len(check['tools']) > 12 else ''}). Cowork uses them from your next task. "
                               "You can delete this message; any keys are stored only on the box.")
                    else:
                        reason = check.get("error") or check.get("note") or "it didn't list any tools"
                        yield (f"Saved **{result['name']}**, but connecting failed just now: {connection_error(reason, request_text)}. "
                               "Check the URL/command and keys, then send the command again (or `/connect mcp "
                               f"{result['name']} off`).")
                    return
                if command.startswith("/connect telegram"):
                    if tier != "owner":
                        yield "Only the owner can manage connections."
                        return
                    words = request_text.split()
                    if len(words) != 3:
                        yield ("Make a bot with **@BotFather** in Telegram (`/newbot`), then send `/connect telegram <the token "
                               "it gives you>`. Send `/connect telegram off` to disconnect it.")
                        return
                    try:
                        state = await self._call(client, "POST", "/api/connections/telegram", json={"token": words[2]})
                    except ValueError as error:
                        yield f"Not connected: {error}"
                        return
                    if not state["connected"]:
                        yield "Telegram is disconnected."
                        return
                    yield (f"Telegram bot **@{state['bot']}** is connected. Open https://t.me/{state['bot']} and send:\n\n"
                           f"`/start {state['pairing_code']}`\n\nOnly the Telegram account that sends this code can use the bot. "
                           "You can delete this message from the chat; the token is stored only on the box.")
                    return
                social_name = command.split()[1] if command.startswith("/connect ") and len(command.split()) > 1 else ""
                if social_name in {"x", "twitter", "instagram", "ig", "tiktok", "bluesky", "github", "scrapecreators"}:
                    if tier != "owner":
                        yield "Only the owner can manage connections."
                        return
                    service = {"twitter": "x", "ig": "instagram"}.get(social_name, social_name)
                    words = request_text.split()[2:]
                    try:
                        result = await self._call(client, "POST", "/api/connections/social", json={"service": service, "words": words})
                    except ValueError as error:
                        yield f"Not saved: {connection_error(error, request_text)}"
                        return
                    state = result["accounts"][service]
                    yield (f"**{service}** is {'connected' if state['connected'] else 'disconnected'}. "
                           + ("You can delete this message from the chat; the sign-in is stored only on the box." if state["connected"] else ""))
                    return
                if command.startswith("/update-code"):
                    parts = request_text.split()
                    if tier != "owner" or len(parts) != 2:
                        yield "Owner only: `/update-code <40-character commit sha>`."
                        return
                    staged = await self._call(client, "POST", "/api/admin/code", json={"ref": parts[1]})
                    yield f"Staged `{staged['staged'][:12]}` ({staged['files']} files). Restart the instance (not recycle) to run it."
                    return
                if command.split()[:2] == ["/connect", "typesafe"]:
                    if tier != "owner":
                        yield "Only the owner can manage connections."
                        return
                    key = request_text.split(None, 2)[2].strip() if len(request_text.split(None, 2)) == 3 else ""
                    if not key:
                        yield "Send `/connect typesafe <your TypeSafe API key>` (it starts with apikey_), or `/connect typesafe off`."
                        return
                    try:
                        info = await self._call(client, "POST", "/api/connections/typesafe", json={"token": key})
                    except ValueError as error:
                        yield f"Not saved: {connection_error(error, request_text)}"
                        return
                    yield ("Jev (TypeSafe) is connected. Cowork will use it for classifying, ranking, scoring and checking "
                           "when that fits, and Claude Code gets the key for TypeSafe builds. You can delete this message from "
                           "the chat (the key is stored only on the box)." if info.get("signed_in") else "Jev (TypeSafe) is disconnected.")
                    return
                if command.startswith("/connect claude"):
                    if tier != "owner":
                        yield "Only the owner can manage connections."
                        return
                    token = request_text.split(None, 2)[2].strip() if len(request_text.split(None, 2)) == 3 else ""
                    if not token:
                        yield ("Run `claude setup-token` on a computer where you use Claude Code, then send "
                               "`/connect claude <the sk-ant-… token it prints>`.")
                        return
                    info = await self._call(client, "POST", "/api/connections/claude", json={"token": token})
                    yield ("Claude Code is connected. Cowork can now hand work to it. You can delete this message from the chat "
                           "(the token is stored only on the box)." if info.get("signed_in") else "Saved, but Claude Code still isn't ready.")
                    return
                if command == "/connect codex":
                    if tier != "owner":
                        yield "Only the owner can manage connections."
                        return
                    await self._status(emit, "Starting Codex sign-in…")
                    login = await self._call(client, "POST", "/api/connections/codex")
                    await self._status(emit, "Codex sign-in started", done=True)
                    output = login.get("output", "").strip() or "(no output yet)"
                    yield ("Open the link below, sign in with your ChatGPT account and enter the code. The box waits up to "
                            "15 minutes; send `/connections` afterwards to confirm.\n\n```\n" + output[-2000:] + "\n```\n\n"
                            "If it says device codes are disabled, enable *device code authorization for Codex* in "
                            "ChatGPT → Settings → Security, then send `/connect codex` again.")
                    return

                if request_text and not command.startswith("/") and command not in {"status", "continue watching"}:
                    waiting = await self._call(client, "GET", "/api/thread/active", params={
                        "project": project, "thread": thread, "requested_by": user.get("email") or ""})
                    if waiting.get("question") and waiting.get("job"):
                        # The running task asked a question: this message is its answer, not a new task.
                        await self._call(client, "POST", f"/api/jobs/{waiting['job']['id']}/answer", json={"answer": request_text})
                        await self._status(emit, "Answer sent; the task continues…")
                        async for piece in self._follow(client, waiting["job"]["id"], emit, body, (user, __request__, __metadata__)):
                            yield piece
                        return

                if command in {"status", "/status", "continue watching"}:
                    active = (await self._call(client, "GET", "/api/thread/active", params={
                        "project": project, "thread": thread, "requested_by": user.get("email") or ""}))["job"]
                    if not active:
                        yield "Nothing is running in this chat right now."
                        return
                    await self._status(emit, "Following the task that's still running…")
                    async for piece in self._follow(client, active["id"], emit, body, (user, __request__, __metadata__)):
                        yield piece
                    return

                await self._status(emit, "Preparing the workspace…")
                attached = await self._upload_inputs(client, project, thread, __files__, images, user.get("email") or "")
                history, used = [], 0
                for message in reversed(messages[:-1]):
                    text, pictures = self._text(message.get("content"))
                    text = re.sub(r"<details[\s\S]*?</details>", "", text).strip()
                    if text.lower().startswith("/connect"):  # sign-ins never go to the model
                        text = "(connection command, hidden)"
                    text = hide_higgsfield_auth(text)
                    entry = f"{message['role'].upper()}: {text}" + (" [image attached]" if pictures else "")
                    if used + len(entry) > self.valves.HISTORY_CHARACTERS:
                        history.append("(earlier messages omitted)")
                        break
                    history.append(entry)
                    used += len(entry)
                parts = []
                if history:
                    parts.append("CONVERSATION SO FAR (earlier turns of this chat):\n" + "\n\n".join(reversed(history)))
                parts.append("CURRENT REQUEST:\n" + (hide_higgsfield_auth(request_text) or "(see attached files)"))
                if attached:
                    parts.append("FILES THE USER JUST ATTACHED (in your workspace):\n" + "\n".join(f"- {p}" for p in attached))
                profile = self.valves.OWNER_PROFILE
                job = await self._call(client, "POST", "/api/jobs", json={
                    "project": project, "task": "\n\n".join(parts), "skill": "chat" if chat else "cowork",
                    "profile": profile if profile in {"fast", "balanced", "deep"} else "balanced",
                    "allow_frontier": tier == "owner" and self.valves.OWNER_ESCALATION, "allow_images": self.valves.ALLOW_IMAGES,
                    "thread": thread, "requested_by": user.get("email") or user.get("name") or ""})
                await self._status(emit, "Queued…")
                async for piece in self._follow(client, job["id"], emit, body, (user, __request__, __metadata__)):
                    yield piece
        except (httpx.HTTPError, ValueError, KeyError) as error:
            await self._status(emit, "Could not reach the agent controller", done=True)
            detail = connection_error(error, request_text) if command.startswith("/connect ") else str(error)
            yield f"Could not reach the agent controller ({type(error).__name__}: {detail[:200]}). It may be restarting; try again in a minute."
