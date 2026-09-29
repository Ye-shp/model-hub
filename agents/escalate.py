"""Hand work Qwen can't do well to Claude Code or Codex, running on this same box in the same folder.

Claude Code signs in with CLAUDE_CODE_OAUTH_TOKEN (from `claude setup-token` on any computer, uses your
Claude plan) or ANTHROPIC_API_KEY. Codex signs in with a ChatGPT device login done from the chat
(`/connect codex`), or OPENAI_API_KEY/CODEX_API_KEY. Both run as the owner's sandbox user.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import time
import uuid

import workspace as ws
from sandbox import EXTRA_PATH, Workspace, trim

TIMEOUT = int(os.environ.get("ESCALATION_TIMEOUT", "1500"))
DAILY = {"claude": int(os.environ.get("CLAUDE_DAILY_TASKS", "30")), "codex": int(os.environ.get("CODEX_DAILY_TASKS", "30"))}
QUIET = {"DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "DISABLE_TELEMETRY": "1",
         "IS_SANDBOX": "1"}
BRIEFING = ("You are being called by a local assistant (a Qwen model) that is working on a task for its owner and "
            "handed this part to you. Work in the current directory; the files there are the shared workspace. "
            "Do the work completely rather than describing it. Put deliverables in files. Finish with a short summary: "
            "what you did, which files you created or changed, and anything left unresolved.\n\nTASK:\n")


def binary(name: str) -> str | None:
    return shutil.which(name, path=f"{EXTRA_PATH}:/usr/local/bin:/usr/bin")


def owner() -> Workspace:
    return Workspace("owner", "connections").prepare()


def token_file():
    import store
    return store.DATA / "claude-token"


def save_claude_token(token: str) -> None:
    """Token from `claude setup-token`, sent with /connect claude. Kept root-only on the box's disk."""
    token = token.strip()
    if not re.fullmatch(r"sk-ant-[A-Za-z0-9_-]{20,300}", token):
        raise ValueError("That doesn't look like a Claude token (it starts with sk-ant-)")
    path = token_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(token)


def claude_env() -> dict:
    env = {k: os.environ[k] for k in ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY") if os.environ.get(k)}
    saved = token_file()
    if saved.is_file():  # a token sent from the chat wins over the instance setting (it is the newer one)
        env["CLAUDE_CODE_OAUTH_TOKEN"] = saved.read_text().strip()
    return {**QUIET, **env}


def codex_env() -> dict:
    return {k: os.environ[k] for k in ("OPENAI_API_KEY", "CODEX_API_KEY") if os.environ.get(k)}


def status() -> dict:
    home = owner().home
    return {
        "claude": {"installed": bool(binary("claude")), "signed_in": bool(claude_env().keys() - QUIET.keys()),
                   "used_today": used_today("claude"), "daily_limit": DAILY["claude"]},
        "codex": {"installed": bool(binary("codex")),
                  "signed_in": bool(codex_env()) or (home / ".codex" / "auth.json").is_file(),
                  "used_today": used_today("codex"), "daily_limit": DAILY["codex"]},
    }


def used_today(kind: str) -> int:
    return ws.query("SELECT COUNT(*) AS n FROM events WHERE kind='escalation' AND detail LIKE ? "
                    "AND datetime(created_at) >= datetime('now','-1 day')", (kind + ":%",))[0]["n"]


def available(kind: str) -> str | None:
    """None if usable, otherwise the reason it isn't."""
    info = status()[kind]
    if not info["installed"]:
        return f"{kind} is not installed on this box"
    if not info["signed_in"]:
        return {"claude": "Claude Code is not signed in (send /connect claude in the chat)",
                "codex": "Codex is not signed in (send /connect codex in the chat)"}[kind]
    if info["used_today"] >= info["daily_limit"]:
        return f"{kind} daily limit reached ({info['daily_limit']} tasks per 24 hours)"
    return None


async def run(kind: str, workspace: Workspace, job_id: str, task: str, timeout: int = TIMEOUT) -> dict:
    reason = available(kind)
    if reason:
        return {"ok": False, "error": reason}
    if workspace.tier != "owner":
        return {"ok": False, "error": "Only the owner's tasks can use Claude or Codex"}
    ws.event(job_id, "escalation", f"{kind}: {task[:300]}")
    started = time.monotonic()
    prompt = BRIEFING + task
    if kind == "claude":
        command = [binary("claude"), "-p", prompt, "--output-format", "json", "--dangerously-skip-permissions",
                   "--max-turns", os.environ.get("CLAUDE_MAX_TURNS", "60")]
        if os.environ.get("CLAUDE_MODEL"):
            command += ["--model", os.environ["CLAUDE_MODEL"]]
        result = await workspace.run(command, timeout=timeout, extra_env=claude_env())
        summary, details = result["output"], {}
        for line in reversed(result["output"].strip().splitlines()):
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if isinstance(data, dict) and "result" in data:
                summary = data.get("result") or ""
                details = {k: data.get(k) for k in ("is_error", "num_turns", "total_cost_usd", "duration_ms")}
                break
    else:
        last = workspace.home / "tmp" / f"codex-{uuid.uuid4().hex}.txt"
        command = [binary("codex"), "exec", "--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox",
                   "--color", "never", "-C", str(workspace.dir), "-o", str(last), prompt]
        if os.environ.get("CODEX_MODEL"):
            command[2:2] = ["-m", os.environ["CODEX_MODEL"]]
        result = await workspace.run(command, timeout=timeout, extra_env=codex_env())
        summary = last.read_text(encoding="utf-8", errors="replace") if last.is_file() else ""
        last.unlink(missing_ok=True)
        details = {}
        if not summary.strip():
            summary = "(no final message) Output tail:\n" + result["output"][-4000:]
    ok = not result["timed_out"] and result["exit_code"] == 0 and not details.get("is_error")
    seconds = round(time.monotonic() - started)
    ws.event(job_id, "escalation-done", f"{kind}: {'finished' if ok else 'failed'} in {seconds}s")
    return {"ok": ok, "agent": kind, "seconds": seconds, "timed_out": result["timed_out"], "exit_code": result["exit_code"],
            **{k: v for k, v in details.items() if v is not None}, "summary": trim(summary, 12000)}


# ---- Codex device login, started from the chat ("/connect codex") ----
LOGIN: dict = {"state": "idle", "output": "", "started": 0}
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


async def codex_login() -> dict:
    if LOGIN["state"] == "waiting" and time.time() - LOGIN["started"] < 900:
        return dict(LOGIN)
    if not binary("codex"):
        return {"state": "failed", "output": "Codex is not installed on this box"}
    account = owner()
    process = await asyncio.create_subprocess_exec(
        *account.argv([binary("codex"), "login", "--device-auth"], cpu_seconds=1200), cwd=str(account.home),
        env=account.env(), stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, start_new_session=True)
    LOGIN.update(state="waiting", output="", started=time.time())

    async def follow():
        try:
            while True:
                line = await asyncio.wait_for(process.stdout.readline(), 900)
                if not line:
                    break
                LOGIN["output"] = (LOGIN["output"] + ANSI.sub("", line.decode(errors="replace")))[-4000:]
            code = await process.wait()
            LOGIN["state"] = "connected" if code == 0 else "failed"
        except asyncio.TimeoutError:
            process.kill()
            LOGIN["state"] = "expired"

    asyncio.get_running_loop().create_task(follow())
    # Give Codex a moment to print the web address and the one-time code.
    for _ in range(40):
        await asyncio.sleep(0.5)
        if re.search(r"https://\S+", LOGIN["output"]) and re.search(r"\b[A-Z0-9]{4}-[A-Z0-9]{4,5}\b", LOGIN["output"]):
            break
        if LOGIN["state"] != "waiting":
            break
    return dict(LOGIN)
