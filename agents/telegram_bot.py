"""Qwen Cowork in Telegram: the owner messages a private bot, each message becomes a Cowork task, and the reply (and any
files it shares) comes back in the same Telegram chat.

Set up from any Cowork chat as the owner: `/connect telegram <bot token from @BotFather>`. The reply shows a pairing
code; send `/start <code>` to the bot from your Telegram account and only that account can use it from then on.

Runs inside the agent controller (console.py starts serve()). It long-polls Telegram, so nothing has to be reachable
from the internet. The token and pairing live in DATA/telegram.json (root-only). Each Telegram chat is a Cowork thread
named tg-<chat id>-<n> in the owner's project; /new starts a fresh one. Links get an instant "which platform" reply and
Cowork studies them with study_link (see study.py), so what it learns lands in the shared knowledge base.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import secrets
from contextlib import suppress
from pathlib import Path

try:
    import httpx2 as httpx
except ImportError:
    import httpx

import links
import store
import workspace as ws

API = "https://api.telegram.org"
PROJECT = "default"  # the owner's project: its knowledge base is shared by all the owner's chats
MAX_DOWNLOAD = 20 * 1024 * 1024  # Telegram's limit for bots downloading files
MAX_UPLOAD = 49 * 1024 * 1024
CHUNK = 3800
HELP = ("Send me a TikTok, Reel, X post or thread, Reddit thread or YouTube link (or a video file up to 20 MB). I'll tell "
        "you which platform it is, study what it says and the top comments, and save anything useful about UGC, "
        "go-to-market and growth to the knowledge base for every future chat. Anything else you write is a normal "
        "Cowork request.\n\n/new starts a fresh conversation, /status shows what's running.")

_watchers: dict[str, asyncio.Task] = {}


# ---------------------------------------------------------------------------------------------
# State (token, owner, polling offset, open threads, replies still owed)
# ---------------------------------------------------------------------------------------------
def _path() -> Path:
    return store.DATA / "telegram.json"


def load() -> dict:
    try:
        return json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}


def save(state: dict) -> None:
    _path().parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(state, handle)


def update(**changes) -> dict:
    state = load()
    state.update(changes)
    save(state)
    return state


def status() -> dict:
    state = load()
    return {"connected": bool(state.get("token")), "bot": state.get("bot"), "paired": bool(state.get("owner_chat")),
            "pairing_code": None if state.get("owner_chat") else state.get("code")}


def _redact(text: str) -> str:
    token = load().get("token")
    return text.replace(token, "[token]") if token else text


# ---------------------------------------------------------------------------------------------
# Telegram Bot API
# ---------------------------------------------------------------------------------------------
async def api(token: str, method: str, data: dict | None = None, files: dict | None = None, timeout: float = 40):
    """One Bot API call; returns its result or raises RuntimeError with Telegram's description."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        if files:
            response = await client.post(f"{API}/bot{token}/{method}", data=data or {}, files=files)
        else:
            response = await client.post(f"{API}/bot{token}/{method}", json=data or {})
    try:
        body = response.json()
    except ValueError:
        raise RuntimeError(f"Telegram answered HTTP {response.status_code}") from None
    if not body.get("ok"):
        raise RuntimeError(f"Telegram: {body.get('description') or response.status_code}")
    return body.get("result")


async def download(token: str, file_id: str) -> bytes:
    info = await api(token, "getFile", {"file_id": file_id})
    async with httpx.AsyncClient(timeout=120) as client:
        response = await client.get(f"{API}/file/bot{token}/{info['file_path']}")
        response.raise_for_status()
        return response.content


async def connect(token: str) -> dict:
    token = token.strip()
    if not re.fullmatch(r"\d{5,12}:[A-Za-z0-9_-]{30,60}", token):
        raise ValueError("That doesn't look like a bot token (from @BotFather it looks like 123456789:AA…)")
    me = await api(token, "getMe")
    with suppress(Exception):
        await api(token, "deleteWebhook", {"drop_pending_updates": False})
    save({"token": token, "bot": me.get("username"), "code": secrets.token_hex(4), "offset": 0,
          "owner_chat": None, "owner_user": None, "threads": {}, "pending": {}})
    return status()


def disconnect() -> dict:
    with suppress(FileNotFoundError):
        _path().unlink()
    return status()


# ---------------------------------------------------------------------------------------------
# Markdown -> Telegram HTML
# ---------------------------------------------------------------------------------------------
def to_html(markdown: str) -> str:
    """The subset of markdown Telegram can show (bold, italic, code, links, headings as bold, bullets)."""
    blocks = []

    def keep(match):
        blocks.append(f"<pre>{html.escape(match.group(2).strip(chr(10)))}</pre>")
        return f"\x00{len(blocks) - 1}\x00"

    text = re.sub(r"<details>.*?</details>", "", markdown or "", flags=re.S)
    text = re.sub(r"```(\w*)\n?(.*?)```", keep, text, flags=re.S)
    text = html.escape(text, quote=False)
    text = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>', text)
    text = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", text, flags=re.M)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"^(\s*)[-*]\s+", r"\1• ", text, flags=re.M)
    return re.sub(r"\x00(\d+)\x00", lambda m: blocks[int(m.group(1))], text)


def pieces(text: str, size: int = CHUNK) -> list[str]:
    """Split a long reply at paragraph, then line, boundaries (Telegram allows 4096 characters per message)."""
    text = (text or "").strip() or "(no reply)"
    out = []
    while len(text) > size:
        cut = text.rfind("\n\n", 0, size)
        cut = cut if cut > size // 3 else text.rfind("\n", 0, size)
        cut = cut if cut > size // 3 else size
        out.append(text[:cut].strip())
        text = text[cut:].strip()
    return out + [text]


async def send(token: str, chat: int, text: str) -> None:
    for piece in pieces(text):
        try:
            await api(token, "sendMessage", {"chat_id": chat, "text": to_html(piece), "parse_mode": "HTML",
                                             "disable_web_page_preview": True})
        except RuntimeError:  # bad markup: send it plain
            await api(token, "sendMessage", {"chat_id": chat, "text": piece[:4096], "disable_web_page_preview": True})


# ---------------------------------------------------------------------------------------------
# Messages in, tasks out
# ---------------------------------------------------------------------------------------------
def thread_for(state: dict, chat: int) -> str:
    return f"tg-{chat}-{state.get('threads', {}).get(str(chat), 1)}"


def message_links(message: dict) -> list[str]:
    text = message.get("text") or message.get("caption") or ""
    found = links.find_urls(text)
    for entity in (message.get("entities") or []) + (message.get("caption_entities") or []):
        if entity.get("type") == "text_link" and entity.get("url") and entity["url"] not in found:
            found.append(entity["url"])
    return found


def attachment(message: dict) -> tuple[str, str, int] | None:
    """(file_id, file name, size) of a video/document/photo in the message."""
    for kind in ("video", "animation", "video_note", "document", "audio", "voice"):
        item = message.get(kind)
        if item:
            extension = {"video": ".mp4", "animation": ".mp4", "video_note": ".mp4", "audio": ".mp3", "voice": ".ogg"}.get(kind, "")
            name = item.get("file_name") or f"telegram-{kind}-{message.get('message_id')}{extension}"
            return item["file_id"], name, item.get("file_size") or 0
    if message.get("photo"):
        largest = max(message["photo"], key=lambda p: p.get("file_size") or 0)
        return largest["file_id"], f"telegram-photo-{message.get('message_id')}.jpg", largest.get("file_size") or 0
    return None


def history(project: str, thread: str, limit: int = 4, characters: int = 6000) -> str:
    rows = ws.query("SELECT task,result,status FROM jobs WHERE project=? AND thread=? ORDER BY created_at DESC, rowid DESC LIMIT ?",
                    (project, thread, limit))
    entries, used = [], 0
    for row in rows:
        request = row["task"].split("CURRENT REQUEST:\n", 1)[-1].split("\n\nFILES THE USER", 1)[0].strip()
        entry = f"USER: {request[:1500]}\n\nASSISTANT: {(row['result'] or '(' + row['status'] + ')')[:2000]}"
        if used + len(entry) > characters:
            break
        entries.append(entry)
        used += len(entry)
    return "\n\n".join(reversed(entries))


async def handle(message: dict) -> None:
    state = load()
    token, chat = state.get("token"), (message.get("chat") or {}).get("id")
    if not token or chat is None or (message.get("chat") or {}).get("type") != "private":
        return
    user = (message.get("from") or {}).get("id")
    text = (message.get("text") or message.get("caption") or "").strip()
    command = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""

    if not state.get("owner_chat"):
        code = state.get("code") or ""
        if code and text.lower() in {code, f"/start {code}"}:
            update(owner_chat=chat, owner_user=user, code=None)
            await send(token, chat, "Paired. This bot now answers only you.\n\n" + HELP)
        else:
            await send(token, chat, "This is a private bot. To pair it, send /start followed by the code shown in your "
                                    "Cowork chat.")
        return
    if chat != state["owner_chat"] or user != state.get("owner_user"):
        return  # strangers get no answer

    if command in {"/start", "/help"}:
        await send(token, chat, HELP)
        return
    if command == "/new":
        threads = state.get("threads", {})
        threads[str(chat)] = threads.get(str(chat), 1) + 1
        update(threads=threads)
        await send(token, chat, "Started a fresh conversation. The knowledge base carries over.")
        return
    thread = thread_for(state, chat)
    if command == "/status":
        rows = ws.query("SELECT id,status,task FROM jobs WHERE project=? AND thread=? AND status IN ('queued','running') "
                        "ORDER BY created_at", (PROJECT, thread))
        await send(token, chat, "Nothing is running." if not rows else "\n".join(
            f"- {r['status']}: {r['task'].split('CURRENT REQUEST:' + chr(10), 1)[-1][:120]}" for r in rows))
        return

    found = message_links(message)
    attached, problems = [], []
    item = attachment(message)
    if item:
        file_id, name, size = item
        if size > MAX_DOWNLOAD:
            problems.append(f"{name} is over Telegram's 20 MB limit for bots: send its link instead.")
        else:
            try:
                import sandbox
                content = await download(token, file_id)
                space = sandbox.Workspace("owner", thread).prepare()
                attached.append(space.relative(space.write_bytes(f"uploads/{Path(name).name[:120]}", content)))
            except Exception as error:
                problems.append(f"Couldn't download {name}: {_redact(str(error))[:200]}")
    if not text and not attached:
        if problems:
            await send(token, chat, "\n".join(problems))
        return

    what = [links.describe(found)] if found else []
    if attached:
        what.append("your file")
    ack = (f"📥 {' + '.join(what)} — studying it now." if what else "On it.") + (" " + " ".join(problems) if problems else "")
    await send(token, chat, ack)
    with suppress(Exception):
        await api(token, "sendChatAction", {"chat_id": chat, "action": "typing"})

    parts = []
    earlier = history(PROJECT, thread)
    if earlier:
        parts.append("CONVERSATION SO FAR (earlier turns of this Telegram chat):\n" + earlier)
    parts.append("CURRENT REQUEST:\n" + (text or "Study the attached file."))
    if attached:
        parts.append("FILES THE USER JUST ATTACHED (in your workspace):\n" + "\n".join(f"- {p}" for p in attached))
    profile = os.environ.get("TELEGRAM_PROFILE", "balanced")
    job = ws.create_job(PROJECT, "\n\n".join(parts), "cowork", profile if profile in {"fast", "balanced", "deep"} else "balanced",
                        allow_images=True, thread=thread, requested_by="telegram")
    pending = load().get("pending", {})
    pending[job] = chat
    update(pending=pending)
    _watch(job, chat)


# ---------------------------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------------------------
def _watch(job: str, chat: int) -> None:
    if job not in _watchers or _watchers[job].done():
        _watchers[job] = asyncio.create_task(watch(job, chat))


async def watch(job: str, chat: int, every: float = 3.0) -> None:
    """Wait for a task to finish, then send its reply and files; follows automatic next phases."""
    typing_at = 0.0
    loop = asyncio.get_running_loop()
    while True:
        rows = ws.query("SELECT status,result,error FROM jobs WHERE id=?", (job,))
        token = load().get("token")
        if not rows or not token:
            break
        row = rows[0]
        if row["status"] in {"queued", "running"}:
            if row["status"] == "running" and loop.time() - typing_at > 8:
                typing_at = loop.time()
                with suppress(Exception):
                    await api(token, "sendChatAction", {"chat_id": chat, "action": "typing"})
            await asyncio.sleep(every)
            continue
        following = ws.query("SELECT id FROM jobs WHERE parent=? ORDER BY created_at LIMIT 1", (job,))
        if row["status"] == "completed":
            text = row["result"] or "(no reply)"
            if following:
                text += "\n\n(The next phase started automatically; I'll send it when it's done.)"
        elif row["status"] == "cancelled":
            text = "Stopped. Files made so far are still in this chat's workspace."
        else:
            text = (f"The task stopped before finishing: {row['error'] or row['status']}\n\n"
                    "Reply **continue** to pick up where it left off.")
        try:
            await send(token, chat, text)
            await send_files(token, chat, job)
        except Exception as error:
            print(f"[telegram] couldn't deliver {job[:8]}: {_redact(str(error))[:200]}", flush=True)
        pending = load().get("pending", {})
        pending.pop(job, None)
        if row["status"] == "completed" and following:
            pending[following[0]["id"]] = chat
            update(pending=pending)
            job = following[0]["id"]
            continue
        update(pending=pending)
        break
    _watchers.pop(job, None)


async def send_files(token: str, chat: int, job: str) -> None:
    for row in ws.query("SELECT name,path,media_type FROM artifacts WHERE job_id=? ORDER BY created_at,rowid", (job,)):
        path = Path(row["path"])
        if not path.is_file():
            continue
        if path.stat().st_size > MAX_UPLOAD:
            await send(token, chat, f"{row['name']} is too big for Telegram (50 MB); it's in the chat's workspace.")
            continue
        photo = (row["media_type"] or "").startswith("image/") and path.stat().st_size < 10 * 1024 * 1024
        await api(token, "sendPhoto" if photo else "sendDocument", {"chat_id": str(chat)},
                  files={"photo" if photo else "document": (row["name"], path.read_bytes(), row["media_type"] or "application/octet-stream")},
                  timeout=180)


# ---------------------------------------------------------------------------------------------
# The polling loop (started by console.py with the worker)
# ---------------------------------------------------------------------------------------------
async def serve(idle: float = 15) -> None:
    for job, chat in load().get("pending", {}).items():  # replies owed from before a restart
        _watch(job, chat)
    failures = 0
    while True:
        state = load()
        token = state.get("token")
        if not token:
            await asyncio.sleep(idle)
            continue
        try:
            updates = await api(token, "getUpdates", {"offset": state.get("offset", 0), "timeout": 50,
                                                      "allowed_updates": ["message"]}, timeout=70)
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as error:
            failures += 1
            if failures in (1, 10) or failures % 100 == 0:
                print(f"[telegram] polling failed ({failures}x): {_redact(str(error))[:200]}", flush=True)
            await asyncio.sleep(min(60, 2 * failures))
            continue
        for item in updates or []:
            update(offset=item["update_id"] + 1)  # never handle an update twice, even if it fails
            if item.get("message"):
                try:
                    await handle(item["message"])
                except Exception as error:
                    print(f"[telegram] message failed: {type(error).__name__}: {_redact(str(error))[:300]}", flush=True)
                    with suppress(Exception):
                        await send(token, item["message"]["chat"]["id"], f"Something went wrong: {type(error).__name__}. Try again.")
