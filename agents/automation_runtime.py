"""Prepare phone dependencies only on explicit setup; keep Hub data and its venv intact."""
from __future__ import annotations

import asyncio
import importlib
import importlib.metadata
import importlib.util
import os
from pathlib import Path
import re
import shutil
import signal
import sys

import store

PACKAGES = (("PyYAML", "yaml", "6.0.3"), ("tzdata", "tzdata", "2026.4"),
            ("uiautomator2", "uiautomator2", "3.7.0"))
ROOT = Path(__file__).resolve().parent.parent
SOURCE_MISSING = "tools/automation source"
SOURCE_FILES = ("__init__.py", "cadence.py", "config.py", "content.py", "device.py",
                "drivers/__init__.py", "drivers/base.py", "drivers/instagram.py", "drivers/tiktok.py")
_LOCK = asyncio.Lock()


def _python_dir() -> Path:
    return store.DATA / "automation" / "python"


def _register() -> None:
    target = _python_dir()
    if target.is_dir() and str(target) not in sys.path:
        sys.path.insert(0, str(target))
        importlib.invalidate_caches()


def ready() -> dict:
    """Read-only readiness; reactivate already installed persistent packages after restart."""
    _register()
    missing = []
    for distribution, module, version in PACKAGES:
        try:
            available = importlib.util.find_spec(module) is not None
            available = available and importlib.metadata.version(distribution) == version
        except (ImportError, ValueError, importlib.metadata.PackageNotFoundError):
            available = False
        if not available:
            missing.append(f"{distribution}=={version}")
    adb = shutil.which("adb")
    if not adb:
        missing.append("adb")
    if not _source_ready():
        missing.append(SOURCE_MISSING)
    return {"ready": not missing, "missing": missing, "adb": adb,
            "python_dir": str(_python_dir())}


def _source_ready() -> bool:
    return all((ROOT / "tools" / "automation" / "bot" / name).is_file() for name in SOURCE_FILES)


async def _ensure_source() -> None:
    if _source_ready():
        return
    # The first update is staged by the OLD whitelist, which lacks tools/automation.
    # Repair only that folder from the commit actually running, never from latest/main.
    ref = ROOT.name
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise RuntimeError("Phone automation source is missing from this checkout. Restore tools/automation "
                           "from the same code version; an in-place overlay repair requires a running commit SHA.")
    import code_update
    async with code_update.httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        response = await client.get(f"https://codeload.github.com/{code_update.REPO}/tar.gz/{ref}")
        response.raise_for_status()
    if len(response.content) > 80_000_000:
        raise RuntimeError("Phone automation source archive is unexpectedly large.")
    code_update.extract(response.content, ROOT, parts=("tools/automation/",))
    if not _source_ready():
        raise RuntimeError("The running commit does not contain complete phone automation source.")


async def _run(args: list[str], *, timeout: float, env: dict | None = None) -> None:
    """No shell, noninteractive preparation with a bounded subprocess lifetime."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env=env or os.environ.copy(), start_new_session=os.name == "posix")
    except OSError as exc:
        raise RuntimeError(f"Phone setup could not start {Path(args[0]).name}: {exc}") from exc
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
        if proc.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(proc.pid, signal.SIGKILL)
                else:
                    proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.communicate(), timeout=10)
        except asyncio.TimeoutError:
            pass
        if isinstance(exc, asyncio.CancelledError):
            raise
        raise RuntimeError(f"Phone setup timed out running {Path(args[0]).name}; retry explicit setup.") from exc
    if proc.returncode:
        detail = (output or b"").decode(errors="replace")[-2000:].strip()
        raise RuntimeError(f"Phone setup failed running {Path(args[0]).name} (exit {proc.returncode}): {detail}")


async def ensure() -> dict:
    """Explicit in-place setup. Never replace the agents environment, database, or memories."""
    async with _LOCK:
        state = ready()
        if state["ready"]:
            return state
        await _ensure_source()
        pins = {f"{name}=={version}" for name, _, version in PACKAGES}
        if pins.intersection(state["missing"]):
            target = _python_dir()
            # The dependency target belongs to automation, never an existing data folder
            # selected by a job. Refuse a symlink redirect before asking pip to write it.
            if target.is_symlink() or target.parent.is_symlink():
                raise RuntimeError("Phone dependency directory must not be a symlink.")
            target.mkdir(parents=True, exist_ok=True)
            await _run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
                        "--no-input", "--no-cache-dir", "--upgrade", "--target", str(target),
                        *(f"{name}=={version}" for name, _, version in PACKAGES)], timeout=300)
            _register()
            importlib.invalidate_caches()
        if not shutil.which("adb"):
            apt = shutil.which("apt-get")
            if not apt or not hasattr(os, "geteuid") or os.geteuid() != 0:
                raise RuntimeError("Phone setup needs controller adb. Install adb on the controller, or run "
                                   "explicit setup on the existing Hub as root; keep its persistent volume.")
            env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
            await _run([apt, "update", "-o", "Acquire::Retries=1", "-o", "Acquire::http::Timeout=20"],
                       timeout=120, env=env)
            await _run([apt, "install", "-y", "--no-install-recommends", "adb"], timeout=180, env=env)
        state = ready()
        if not state["ready"]:
            raise RuntimeError("Phone setup finished but dependencies remain unavailable: "
                               + ", ".join(state["missing"]) + ". Retry setup after restarting the same instance.")
        return state
