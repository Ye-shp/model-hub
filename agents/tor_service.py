"""Runs the Tor daemon from the controller when the image's supervisor doesn't.

Images built from PR #8 on start Tor in supervise.mjs and pass TOR_SOCKS_SOCKET; then this module does nothing.
A box still on an older image gets the same service from here, as app code, so the Tor reader works after a plain
/update-code and restart (no recycle, so nothing on /workspace is lost):

- the Tor kit (tools/tor) comes from the staged code; if the code was staged by an older updater that skipped
  tools/tor, the kit for the active commit is fetched once into TOR_HOME/kit;
- setup.sh --install-only installs the pinned, checksum-verified Tor into TOR_HOME (on the data disk, root-only);
- Tor runs in the foreground with only the private Unix SOCKS socket, is restarted if it exits, and dies with the
  controller (so a restarted controller never finds a second Tor holding the data folder).
"""
from __future__ import annotations

import asyncio
import ctypes
import os
import signal
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL_SECONDS = 300
_state = {"state": "off", "detail": "", "managed_by": None}


def status() -> dict:
    return dict(_state)


def _set(state: str, detail: str = "") -> None:
    _state.update(state=state, detail=detail)
    print(f"[tor] {state}{': ' + detail if detail else ''}", flush=True)


def tor_home() -> Path | None:
    if os.environ.get("TOR_HOME"):
        return Path(os.environ["TOR_HOME"])
    root = os.environ.get("HUB_ROOT_DATA_DIR")
    return Path(root) / "tor" if root else None


def kit_dir() -> Path:
    """tools/tor beside the running code, or the copy fetched into TOR_HOME/kit."""
    bundled = HERE.parent / "tools" / "tor"
    if (bundled / "fetch.py").is_file():
        return bundled
    home = tor_home()
    if home and (home / "kit" / "fetch.py").is_file():
        return home / "kit"
    return bundled


async def _fetch_kit(home: Path) -> Path:
    """Download tools/tor for the active staged commit (the code was staged without it)."""
    import code_update
    try:
        import httpx2 as httpx
    except ImportError:
        import httpx
    code_root = Path(os.environ.get("HUB_CODE_DIR") or "")
    sha = (code_root / "active").read_text().strip() if str(code_root) not in {"", "."} else ""
    if len(sha) != 40:
        raise RuntimeError("tools/tor is missing and no staged commit is active")
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        response = await client.get(f"https://codeload.github.com/{code_update.REPO}/tar.gz/{sha}")
        response.raise_for_status()
    staging = home / "kit.tmp"
    await asyncio.to_thread(_replace_kit, response.content, staging, home / "kit")
    if not (home / "kit" / "fetch.py").is_file():
        raise RuntimeError(f"commit {sha[:7]} has no tools/tor")
    return home / "kit"


def _replace_kit(archive: bytes, staging: Path, final: Path) -> None:
    import shutil
    import code_update
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    code_update.extract(archive, staging, parts=("tools/tor/",))
    shutil.rmtree(final, ignore_errors=True)
    (staging / "tools" / "tor").rename(final)
    shutil.rmtree(staging, ignore_errors=True)


def _die_with_parent() -> None:
    """Child side, before exec: SIGTERM when the controller exits (survives the exec in torctl.sh)."""
    try:
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except OSError:
        pass


def _stop_stale(home: Path) -> None:
    """A Tor left over from an earlier controller (should not happen with PDEATHSIG, but the pid file says)."""
    pid_file = home / "run" / "tor.pid"
    try:
        pid = int(pid_file.read_text().strip())
        if Path(f"/proc/{pid}/exe").resolve() == (home / "bin" / "tor").resolve():
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


async def _run(cmd: list[str], env: dict, timeout: float | None) -> int:
    process = await asyncio.create_subprocess_exec(*cmd, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                   preexec_fn=_die_with_parent)
    try:
        async with asyncio.timeout(timeout):
            return await process.wait()
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        raise


async def _serve(home: Path, socket_path: Path) -> None:
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": str(home),
           "TOR_HOME": str(home), "TOR_SOCKS_SOCKET": str(socket_path)}
    try:
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        home.chmod(0o700)
        kit = kit_dir()
        if not (kit / "setup.sh").is_file():
            _set("starting", "fetching the Tor kit for the active code")
            kit = await _fetch_kit(home)
        _set("starting", "installing Tor (first start downloads about 1 MB)")
        code = await _run(["/bin/bash", str(kit / "setup.sh"), "--install-only"], env, INSTALL_SECONDS)
        if code != 0:
            _set("unavailable", f"installer exited with code {code}")
            return
    except asyncio.CancelledError:
        raise
    except Exception as error:
        _set("unavailable", f"{type(error).__name__}: {str(error)[:200]}")
        return
    failures = 0
    loop = asyncio.get_running_loop()
    while True:
        _stop_stale(home)
        started = loop.time()
        _set("running", f"SOCKS on {socket_path}")
        code = await _run(["/bin/bash", str(kit / "torctl.sh"), "foreground"], env, None)
        failures = 1 if loop.time() - started > 300 else failures + 1
        delay = min(300, 5 * 2 ** (failures - 1))
        _set("restarting", f"tor exited (code {code}); restart in {delay}s, see {home / 'run' / 'tor.log'}")
        await asyncio.sleep(delay)


def start() -> asyncio.Task | None:
    """Call from the controller's startup. Returns the task to cancel at shutdown, or None."""
    if os.environ.get("TOR_SOCKS_SOCKET"):
        _state.update(state="running", detail="managed by the supervisor", managed_by="supervisor")
        return None
    home = tor_home()
    if home is None or os.geteuid() != 0:
        _state.update(state="off", detail="not on the hub box")
        return None
    socket_path = home / "run" / "socks.sock"
    os.environ["TOR_HOME"] = str(home)
    os.environ["TOR_SOCKS_SOCKET"] = str(socket_path)  # read by tor_fetch.read_page and tools/tor/fetch.py
    _state["managed_by"] = "controller"
    return asyncio.create_task(_serve(home, socket_path))
