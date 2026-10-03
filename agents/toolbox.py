"""Free research, media and social tools for Qwen Cowork: their install, their sign-ins, and running them.

The Python packages live in their own virtualenv next to the chat folders (TOOLS_DIR, default /workspace/tools),
installed on the controller's first start and kept across restarts, so the hub image doesn't need rebuilding
when a tool changes. Everything here is free: no paid APIs. Sign-ins the owner adds from a chat
(/connect x ..., /connect instagram ..., /connect bluesky ..., /connect github ...) are kept root-only in
DATA/social.json.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import store

HERE = Path(__file__).resolve().parent
TOOLS_DIR = Path(os.environ.get("TOOLS_DIR") or Path(os.environ.get("COWORK_ROOT", "/workspace/cowork")).parent / "tools")
VENV = TOOLS_DIR / "venv"
PYTHON = VENV / "bin" / "python"
WHISPER_DIR = TOOLS_DIR / "models" / "faster-whisper-small"
SCRIPTS = HERE / "tools"
LAST30DAYS = HERE / "vendor" / "last30days" / "scripts" / "last30days.py"

# Pinned, all free and open source (versions current as of October 2026).
PACKAGES = [
    "yt-dlp[default,curl-cffi]>=2026.8.19",  # download/inspect videos; curl-cffi lets it pass as Chrome (TikTok needs it).
                                         # Not pinned: sites change often, so it's upgraded weekly (refresh_ytdlp)
    "scenedetect==0.7.1",                # shot boundaries -> cut rate, one keyframe per shot
    "opencv-python-headless==5.0.0.93",  # needed by scenedetect
    "rapidocr==3.9.2",                   # on-screen text
    "onnxruntime==1.30.0",               # needed by rapidocr
    "faster-whisper==1.2.1",             # voiceover transcript (CPU)
    "shazamio==0.8.1",                   # which song/sound a video uses
    "twscrape==0.20.1",                  # X search, trends, profiles (with your X cookies)
    "twikit==2.3.3",                     # X posting (with your X cookies)
    "instaloader==4.15.3",               # public Instagram profiles and posts
    "pytrends==4.9.2",                   # Google Trends
]
WHISPER_REPO, WHISPER_REVISION = "Systran/faster-whisper-small", "536b0662742c02347bc0e980a01041f333bce120"
VERSION = "2"  # bump to reinstall after changing PACKAGES

STATE = {"state": "unknown", "detail": "", "at": 0.0}
_lock = threading.Lock()


# ---------------------------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------------------------
def marker() -> Path:
    return TOOLS_DIR / f"installed-v{VERSION}"


def ready() -> bool:
    return marker().is_file() and PYTHON.is_file()


def status() -> dict:
    if ready():
        return {"state": "ready", "dir": str(TOOLS_DIR)}
    return {**STATE, "dir": str(TOOLS_DIR)}


def install(force: bool = False) -> dict:
    """Create the tools virtualenv and download the transcription model. Safe to call repeatedly."""
    with _lock:
        if ready() and not force:
            STATE.update(state="ready", detail="", at=time.time())
            return status()
        STATE.update(state="installing", detail="creating the tools environment", at=time.time())
        try:
            TOOLS_DIR.mkdir(parents=True, exist_ok=True)
            os.chmod(TOOLS_DIR, 0o755)  # sandbox users run these tools, so they must be able to read them
            base = shutil.which("python3.12") or shutil.which("python3") or sys.executable
            if not PYTHON.is_file():
                subprocess.run([base, "-m", "venv", str(VENV)], check=True, capture_output=True, timeout=300)
            STATE["detail"] = "installing packages (a few minutes, once)"
            run = subprocess.run([str(PYTHON), "-m", "pip", "install", "--no-cache-dir", "--disable-pip-version-check",
                                  "-q", *PACKAGES], capture_output=True, text=True, timeout=1800)
            if run.returncode != 0:
                raise RuntimeError(run.stderr[-1500:] or run.stdout[-1500:])
            STATE["detail"] = "downloading the transcription model"
            if not (WHISPER_DIR / "model.bin").is_file():
                code = ("from huggingface_hub import snapshot_download; "
                        f"snapshot_download({WHISPER_REPO!r}, revision={WHISPER_REVISION!r}, local_dir={str(WHISPER_DIR)!r})")
                run = subprocess.run([str(PYTHON), "-c", code], capture_output=True, text=True, timeout=1800)
                if run.returncode != 0:
                    raise RuntimeError("model download failed: " + run.stderr[-800:])
            for folder, dirs, files in os.walk(TOOLS_DIR):  # readable by the sandbox users
                for name in dirs:
                    os.chmod(os.path.join(folder, name), 0o755)
                for name in files:
                    path = os.path.join(folder, name)
                    if not os.path.islink(path):
                        os.chmod(path, os.stat(path).st_mode | 0o444)
            marker().write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
            STATE.update(state="ready", detail="", at=time.time())
        except Exception as error:
            STATE.update(state="failed", detail=f"{type(error).__name__}: {str(error)[:1500]}", at=time.time())
        return status()


def refresh_ytdlp(max_age_days: float = 7) -> None:
    """Upgrade yt-dlp when the installed copy is over a week old (TikTok/YouTube break old versions)."""
    stamp = TOOLS_DIR / "ytdlp-updated"
    try:
        if stamp.is_file() and time.time() - stamp.stat().st_mtime < max_age_days * 86400:
            return
        run = subprocess.run([str(PYTHON), "-m", "pip", "install", "-q", "--no-cache-dir", "--disable-pip-version-check", "-U",
                              "yt-dlp[default,curl-cffi]"], capture_output=True, text=True, timeout=600)
        if run.returncode == 0:
            stamp.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    except (OSError, subprocess.SubprocessError):
        pass


def install_in_background() -> None:
    def work():
        install()
        if ready():
            refresh_ytdlp()
    threading.Thread(target=work, name="toolbox-install", daemon=True).start()


def wait_ready(seconds: float = 900) -> None:
    """Block until the tools are installed (installing them if needed)."""
    if ready():
        return
    deadline = time.time() + seconds
    if STATE["state"] != "installing":
        install_in_background()
        time.sleep(1)
    while time.time() < deadline:
        if ready():
            return
        if STATE["state"] == "failed":
            raise RuntimeError("The research tools failed to install: " + STATE["detail"][:400])
        time.sleep(3)
    raise RuntimeError("The research tools are still installing; try again in a few minutes")


# ---------------------------------------------------------------------------------------------
# Sign-ins (free accounts the owner connects)
# ---------------------------------------------------------------------------------------------
SERVICES = {
    "x": ("username", "auth_token", "ct0"),
    "instagram": ("user_id", "access_token"),
    "tiktok": ("account_id", "access_token"),
    "bluesky": ("handle", "app_password"),
    "github": ("token",),
    "scrapecreators": ("key",),
}
HELP = {
    "x": "`/connect x <your X username> auth_token=<…> ct0=<…>` (the two cookies from x.com: browser dev tools → Application → Cookies)",
    "instagram": "`/connect instagram <Instagram user id> <long-lived access token>` (a free Meta developer app with the Instagram API, "
                 "professional account)",
    "tiktok": "`/connect tiktok <OAuth open_id> <access token>` (an authorized TikTok developer app with video.list and "
              "user.info.basic scopes; the account ID is open_id, not your username; this enables metrics, not publishing)",
    "bluesky": "`/connect bluesky <handle> <app password>` (Bluesky → Settings → App passwords)",
    "github": "`/connect github <token>` (a free fine-grained token with no permissions is enough)",
    "scrapecreators": "`/connect scrapecreators <key>` (optional: their free key has 100 calls in total, for TikTok/Instagram search)",
}


def _path() -> Path:
    return store.DATA / "social.json"


def credentials() -> dict:
    try:
        return json.loads(_path().read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_credentials(service: str, values: dict) -> dict:
    if service not in SERVICES:
        raise ValueError("Unknown service: " + ", ".join(SERVICES))
    missing = [k for k in SERVICES[service] if not str(values.get(k, "")).strip()]
    if missing:
        raise ValueError(f"Missing {', '.join(missing)}. Use: {HELP[service]}")
    clean = {k: str(values[k]).strip().lstrip("@") for k in SERVICES[service]}
    if any(len(v) > 600 or any(c in v for c in "\n\r\x00") for v in clean.values()):
        raise ValueError("A value is too long or contains line breaks")
    data = credentials()
    data[service] = {**clean, "saved_at": store.now()}
    store.DATA.mkdir(parents=True, exist_ok=True)
    path = _path()
    fd = os.open(path.with_suffix(".tmp"), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    os.replace(path.with_suffix(".tmp"), path)
    return connected()


def forget(service: str) -> dict:
    data = credentials()
    data.pop(service, None)
    fd = os.open(_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle)
    return connected()


def connected() -> dict:
    data = credentials()
    return {name: {"connected": name in data, "since": data.get(name, {}).get("saved_at"),
                   **({"username": data[name].get("username") or data[name].get("handle")} if name in data else {})}
            for name in SERVICES}


def parse_connect(service: str, words: list[str]) -> dict:
    """Values from '/connect <service> ...' words: key=value pairs, or positional in SERVICES order."""
    fields, values, positional = SERVICES.get(service), {}, []
    if not fields:
        raise ValueError("Unknown service")
    for word in words:
        key, sep, value = word.partition("=")
        if sep and key.lower() in fields:
            values[key.lower()] = value
        else:
            positional.append(word)
    for field in fields:
        if field not in values and positional:
            values[field] = positional.pop(0)
    return values


# ---------------------------------------------------------------------------------------------
# Running the tool scripts
# ---------------------------------------------------------------------------------------------
def social_env() -> dict:
    """Environment for agents/tools/social.py and last30days (root-only: holds the sign-ins)."""
    data = credentials()
    env = {"PATH": f"{VENV / 'bin'}:/usr/local/bin:/usr/bin:/bin", "HOME": str(TOOLS_DIR / "home"),
           "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "HF_HUB_DISABLE_TELEMETRY": "1", "SOCIAL_STATE_DIR": str(store.DATA / "social-state")}
    if "x" in data:
        env.update(AUTH_TOKEN=data["x"]["auth_token"], CT0=data["x"]["ct0"], X_USERNAME=data["x"]["username"])
    if "instagram" in data:
        env.update(IG_USER_ID=data["instagram"]["user_id"], IG_ACCESS_TOKEN=data["instagram"]["access_token"])
    if "bluesky" in data:
        env.update(BSKY_HANDLE=data["bluesky"]["handle"], BSKY_APP_PASSWORD=data["bluesky"]["app_password"])
    if "github" in data:
        env["GITHUB_TOKEN"] = data["github"]["token"]
    if "scrapecreators" in data:
        env["SCRAPECREATORS_API_KEY"] = data["scrapecreators"]["key"]
    for key in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "SSL_CERT_FILE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    (TOOLS_DIR / "home").mkdir(parents=True, exist_ok=True)
    return env


async def run_social(command: str, args: dict, timeout: int = 240) -> dict:
    """Run one command of agents/tools/social.py in the tools environment (as the controller, with the sign-ins)."""
    import asyncio
    await asyncio.to_thread(wait_ready)
    process = await asyncio.create_subprocess_exec(
        str(PYTHON), str(SCRIPTS / "social.py"), command, json.dumps(args), env=social_env(),
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True)
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return {"ok": False, "error": f"{command} took longer than {timeout} s"}
    text = out.decode(errors="replace").strip().splitlines()
    for line in reversed(text):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return {"ok": False, "error": (err.decode(errors="replace")[-1200:] or "no output").strip()}
