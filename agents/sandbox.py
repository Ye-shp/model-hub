"""Per-chat workspaces on the GPU box, and a shell that runs as an unprivileged user.

Every Cowork chat gets its own folder. Commands the agent runs there execute as a separate Unix
user with no capabilities and resource limits, so they can't read the hub's keys, the chat site's
database or another person's files:
  owner          -> "cowork"
  friend-<id>    -> a Unix user of the same name, one per invited friend (created on first use)
  guest          -> "guest", the older shared account for friends (kept for existing chats)
The controller itself (root) does the file reads/writes, confined to the chat folder.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(os.environ.get("COWORK_ROOT", "/workspace/cowork"))
# Fixed accounts. Tests may add "friend" here to run every friend account as one existing user.
USERS = {"owner": os.environ.get("COWORK_OWNER_USER", "cowork"), "guest": os.environ.get("COWORK_GUEST_USER", "guest")}
OUTPUT_LIMIT = 10_000  # characters of command output returned to the model
READ_LIMIT = 12_000
IS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0
EXTRA_PATH = os.environ.get("COWORK_EXTRA_PATH", "/opt/cli/bin")
# Refuse new work when the disk is nearly full: a full disk once broke the chat site and the console.
MIN_FREE_GB = float(os.environ.get("COWORK_MIN_FREE_GB", "4"))
# Each friend's folders together may use this much (the owner is not limited).
FRIEND_QUOTA_GB = float(os.environ.get("COWORK_FRIEND_QUOTA_GB", "5"))
FRIEND_UID_START = 1600
_user_lock = threading.Lock()


def friend_account(email: str) -> str:
    """Stable account name for an invited friend (also their Unix user name and project id)."""
    email = (email or "").strip().lower()
    if not email:
        raise ValueError("A friend account needs an email address")
    return "friend-" + hashlib.sha256(email.encode()).hexdigest()[:10]


def is_account(name: str) -> bool:
    return name in USERS or bool(re.fullmatch(r"friend-[0-9a-f]{10}", name or ""))


def _registry() -> Path:
    import store
    return store.DATA / "sandbox-users.json"


def unix_user(account: str) -> str:
    """The Unix user for an account, creating a friend's user (with a stable UID) when it's missing."""
    if account in USERS:
        return USERS[account]
    if not is_account(account):
        raise ValueError("Unknown workspace account")
    if "friend" in USERS:  # tests
        return USERS["friend"]
    if not IS_ROOT:
        return account
    import pwd
    with _user_lock:
        try:
            pwd.getpwnam(account)
            return account
        except KeyError:
            pass
        path = _registry()
        try:
            uids = json.loads(path.read_text())
        except (FileNotFoundError, ValueError):
            uids = {}
        uid = uids.get(account) or max([FRIEND_UID_START - 1, *uids.values()]) + 1
        # The container's user list is rebuilt on a new image; the UID is kept so old files stay theirs.
        subprocess.run(["useradd", "--uid", str(uid), "--no-create-home", "--shell", "/bin/bash", account],
                       check=True, capture_output=True, timeout=30)
        uids[account] = uid
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(uids, indent=1))
        return account


def free_gb(path: Path | None = None) -> float:
    target = path or ROOT
    while not target.exists() and target != target.parent:
        target = target.parent
    return shutil.disk_usage(target).free / 1024**3


_sizes: dict[str, tuple[float, int]] = {}


def folder_bytes(path: Path, max_age: float = 60) -> int:
    """Total size of a folder (cached for a minute; du is fast but not free)."""
    key = str(path)
    cached = _sizes.get(key)
    if cached and time.monotonic() - cached[0] < max_age:
        return cached[1]
    size = 0
    try:
        out = subprocess.run(["du", "-sb", str(path)], capture_output=True, text=True, timeout=20)
        size = int(out.stdout.split()[0]) if out.stdout else 0
    except (subprocess.TimeoutExpired, ValueError, IndexError, FileNotFoundError):
        for folder, _, files in os.walk(path):
            for name in files:
                try:
                    size += os.lstat(os.path.join(folder, name)).st_size
                except OSError:
                    pass
    _sizes[key] = (time.monotonic(), size)
    return size


CLEANUP = re.compile(r"^(rm|rmdir|du|ls|df|find|cd|pwd)\b")


def is_cleanup(command: str) -> bool:
    """True for commands that only inspect or delete files (allowed even when the disk is full)."""
    parts = [p.strip() for p in re.split(r"&&|\|\||;|\n", command) if p.strip()]
    return bool(parts) and all(CLEANUP.match(p) for p in parts) and ">" not in command


def space_problem(account: str) -> str | None:
    """Why new work can't write to disk right now, or None."""
    free = free_gb()
    if free < MIN_FREE_GB:
        return (f"The server's disk is nearly full ({free:.1f} GB free). Delete large files you no longer need "
                "(for example with rm), or ask the owner to clear old chat folders in the console.")
    if account.startswith("friend-") or account == "guest":
        used = folder_bytes(ROOT / account) / 1024**3
        if used > FRIEND_QUOTA_GB:
            return (f"This account's files use {used:.1f} GB, over its {FRIEND_QUOTA_GB:g} GB allowance. "
                    "Delete files you no longer need (rm) before creating more.")
    return None


def clean_thread(thread: str) -> str:
    thread = re.sub(r"[^A-Za-z0-9_-]", "-", thread or "")[:80].strip("-")
    if not thread:
        raise ValueError("Missing workspace thread")
    return thread


def trim(text: str, limit: int = OUTPUT_LIMIT) -> str:
    """Keep the start and the end of long output (errors are usually at the end)."""
    if len(text) <= limit:
        return text
    head = limit // 5
    return text[:head] + f"\n… [{len(text) - limit} characters omitted] …\n" + text[-(limit - head):]


class Workspace:
    def __init__(self, tier: str, thread: str):
        """tier is the account: "owner", "guest" or "friend-<id>"."""
        if not is_account(tier):
            raise ValueError("Unknown workspace tier")
        self.tier, self.thread = tier, clean_thread(thread)
        self.user = unix_user(tier)
        self.base = ROOT / tier
        self.home = self.base / "home"
        self.dir = self.base / "threads" / self.thread
        self.uid = self.gid = None
        if IS_ROOT:
            import pwd
            try:
                entry = pwd.getpwnam(self.user)
            except KeyError:
                raise RuntimeError(f"Sandbox user {self.user!r} is missing; the shell is disabled") from None
            self.uid, self.gid = entry.pw_uid, entry.pw_gid

    @property
    def is_owner(self) -> bool:
        return self.tier == "owner"

    # ---- setup and ownership ----
    def prepare(self) -> "Workspace":
        for folder in (self.base, self.home, self.home / "tmp", self.base / "threads", self.dir):
            folder.mkdir(parents=True, exist_ok=True)
            self._own(folder)
        if IS_ROOT:
            ROOT.chmod(0o755)
            self.base.chmod(0o700)
        return self

    def _own(self, path: Path):
        if IS_ROOT:
            os.chown(path, self.uid, self.gid, follow_symlinks=False)

    def resolve(self, path: str) -> Path:
        """A path inside this chat's folder (relative paths are relative to it)."""
        if not path or "\x00" in path:
            raise ValueError("Give a file path")
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.dir / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.dir.resolve()):
            raise ValueError(f"Only files inside the workspace ({self.dir}) can be used")
        return resolved

    def relative(self, path: Path) -> str:
        return str(path.relative_to(self.dir.resolve()))

    # ---- files ----
    def listing(self, path: str = ".", depth: int = 2, limit: int = 300) -> str:
        top = self.resolve(path)
        if not top.exists():
            return f"{path} does not exist"
        lines = []
        def walk(folder: Path, level: int):
            for entry in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name)):
                if len(lines) >= limit:
                    return
                if entry.name in {".git", "node_modules", "__pycache__", ".venv"}:
                    lines.append("  " * level + entry.name + "/ (skipped)")
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    lines.append("  " * level + entry.name + "/")
                    if level + 1 < depth:
                        walk(entry, level + 1)
                else:
                    size = entry.lstat().st_size
                    lines.append("  " * level + f"{entry.name} ({size:,} bytes)")
        if top.is_dir():
            walk(top, 0)
        else:
            lines.append(f"{top.name} ({top.stat().st_size:,} bytes)")
        if len(lines) >= limit:
            lines.append(f"… listing stopped at {limit} entries")
        return "\n".join(lines) or "(empty folder)"

    def read_text(self, path: str, offset: int = 1, limit: int = 400) -> str:
        target = self.resolve(path)
        if not target.is_file():
            raise ValueError(f"{path} is not a file")
        raw = target.read_bytes()
        if b"\x00" in raw[:4096]:
            kind = mimetypes.guess_type(target.name)[0] or "binary"
            return f"{path} is a binary file ({kind}, {len(raw):,} bytes). Inspect it with a shell command instead."
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = max(1, offset)
        chosen = lines[start - 1:start - 1 + max(1, min(limit, 2000))]
        text = "\n".join(f"{start + i:>5}  {line}" for i, line in enumerate(chosen))
        if len(text) > READ_LIMIT:
            text = text[:READ_LIMIT] + "\n… [truncated; read a smaller range]"
        end = start + len(chosen) - 1
        return f"{path}: lines {start}-{end} of {len(lines)}\n{text}"

    def write_bytes(self, path: str, content: bytes) -> Path:
        target = self.resolve(path)
        problem = space_problem(self.tier)
        if problem:
            raise ValueError(problem)
        missing = []
        parent = target.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        target.parent.mkdir(parents=True, exist_ok=True)
        for folder in missing:
            self._own(folder)
        if target.is_symlink():
            raise ValueError("Refusing to write through a symbolic link")
        target.write_bytes(content)
        self._own(target)
        return target

    def write_text(self, path: str, content: str) -> str:
        target = self.write_bytes(path, content.encode("utf-8"))
        return f"Wrote {self.relative(target)} ({len(content):,} characters)"

    def edit(self, path: str, old: str, new: str, replace_all: bool = False) -> str:
        target = self.resolve(path)
        text = target.read_text(encoding="utf-8")
        count = text.count(old) if old else 0
        if count == 0:
            raise ValueError("old_text was not found; read the file and copy the exact text")
        if count > 1 and not replace_all:
            raise ValueError(f"old_text appears {count} times; include more surrounding text or set replace_all")
        self.write_text(path, text.replace(old, new) if replace_all else text.replace(old, new, 1))
        return f"Edited {self.relative(target)} ({count if replace_all else 1} replacement)"

    # ---- commands ----
    def env(self, extra: dict | None = None) -> dict:
        local = self.home / ".local"
        env = {
            "PATH": f"{local}/bin:{self.home}/.npm-global/bin:{EXTRA_PATH}:/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home), "USER": self.user, "LOGNAME": self.user,
            "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb", "TMPDIR": str(self.home / "tmp"),
            "PYTHONUNBUFFERED": "1", "PYTHONUSERBASE": str(local), "PIP_USER": "1", "PIP_BREAK_SYSTEM_PACKAGES": "1",
            "PIP_DISABLE_PIP_VERSION_CHECK": "1", "NPM_CONFIG_PREFIX": str(self.home / ".npm-global"), "MPLBACKEND": "Agg",
            "NO_COLOR": "1",
        }
        return {**env, **(extra or {})}

    def argv(self, command: list[str], cpu_seconds: int) -> list[str]:
        # Lower CPU priority so the agent's jobs never slow the model servers or the website.
        limits = ["nice", "-n", "10", "prlimit", "--nproc=1024", "--nofile=4096", f"--fsize={4 * 1024**3}",
                  f"--cpu={cpu_seconds}", "--"]
        if not IS_ROOT or not shutil.which("prlimit"):
            limits = []
        if not IS_ROOT:
            return limits + command
        return ["setpriv", f"--reuid={self.uid}", f"--regid={self.gid}", "--init-groups", "--no-new-privs",
                "--inh-caps=-all", "--bounding-set=-all", "--"] + limits + command

    async def run(self, command: list[str] | str, timeout: int = 120, extra_env: dict | None = None,
                  cwd: Path | None = None) -> dict:
        if isinstance(command, str):
            command = ["bash", "-c", command]
        problem = space_problem(self.tier)
        # Deleting files must still work on a full disk, so only refuse commands that don't look like cleanup.
        if problem and not is_cleanup(command[-1] if command[:2] == ["bash", "-c"] else ""):
            return {"exit_code": None, "timed_out": False, "output": "Not run: " + problem}
        process = await asyncio.create_subprocess_exec(
            *self.argv(command, cpu_seconds=max(60, timeout * 4)), cwd=str(cwd or self.dir), env=self.env(extra_env),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=True)
        chunks, size, timed_out = [], 0, False

        async def pump():
            nonlocal size
            while True:
                block = await process.stdout.read(65536)
                if not block:
                    return
                if size < 2_000_000:  # keep memory bounded; the tail is what matters most
                    chunks.append(block)
                else:
                    chunks[-1] = (chunks[-1] + block)[-1_000_000:]
                size += len(block)

        reader = asyncio.create_task(pump())
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            # Also stops anything the command left running in the background.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.wait_for(reader, 5)
            except asyncio.TimeoutError:
                reader.cancel()
        output = b"".join(chunks).decode("utf-8", errors="replace")
        return {"exit_code": None if timed_out else process.returncode, "timed_out": timed_out, "output": output}
