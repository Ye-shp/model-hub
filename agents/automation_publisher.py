"""One-shot native phone publication, with a durable boundary before Submit.

The worker supplies an immutable approved media snapshot and holds the device
lease. No bridge, project sandbox, scheduler, or legacy retry loop is involved.
App layouts that cannot prove the account/media stay ready for manual posting.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import re
import shlex
import shutil
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Callable


_NAMESPACE = "_modelhub_phone_automation"
_IMPORT_LOCK = threading.Lock()
_PACKAGES = {"instagram": "com.instagram.android", "tiktok": "com.zhiliaoapp.musically"}
_VIDEO = {".mp4", ".mov", ".m4v"}


def _scaffold_device():
    """Import the local scaffold without claiming the generic `bot` package."""
    path = Path(__file__).resolve().parents[1] / "tools" / "automation" / "bot"
    with _IMPORT_LOCK:
        package = sys.modules.get(_NAMESPACE)
        if package is not None and Path(package.__file__).resolve() != path / "__init__.py":
            raise RuntimeError("The private phone driver namespace is already occupied")
        if package is None:
            spec = importlib.util.spec_from_file_location(_NAMESPACE, path / "__init__.py",
                                                         submodule_search_locations=[str(path)])
            package = importlib.util.module_from_spec(spec)
            sys.modules[_NAMESPACE] = package
            try:
                spec.loader.exec_module(package)
            except BaseException:
                sys.modules.pop(_NAMESPACE, None)
                raise
        return importlib.import_module(f"{_NAMESPACE}.device").DeviceController


def _device(settings: dict):
    import automation_runtime
    import store
    runtime = automation_runtime.ready()
    if not runtime["ready"]:
        raise RuntimeError("Phone dependencies are not ready; run the phone setup tool first")
    DeviceController = _scaffold_device()
    return DeviceController(settings["device_serial"], dry_run=False,
                            adb_bin=settings.get("adb_bin") or runtime["adb"] or "adb",
                            screenshots_dir=store.DATA / "automation" / "screenshots")


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot(post: dict, account: dict, settings: dict) -> tuple[str, str, Path, str]:
    if settings.get("enabled") is not True or settings.get("dry_run") is not False:
        raise ValueError("Live phone posting must be explicitly enabled by the worker")
    if settings.get("transport", "direct_adb") != "direct_adb" or not settings.get("device_serial"):
        raise ValueError("Configure a direct ADB device through Tailscale or SSH first")
    platform = post.get("platform")
    if platform not in _PACKAGES or account.get("platform", platform) != platform:
        raise ValueError("The post and phone account must use the same supported platform")
    username = str(account.get("username") or "").lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", username):
        raise ValueError("A valid configured account username is required")
    if account.get("health", "ok") in {"paused", "warning", "shadowbanned"}:
        raise PermissionError("This account is on hold; review its health before posting")
    paths, hashes = post.get("media") or [], post.get("media_hashes") or []
    if len(paths) != 1 or len(hashes) != 1:
        raise ValueError("Native phone posting currently supports exactly one approved video")
    path = Path(paths[0])
    if post.get("kind") not in {"reel", "video"} or path.suffix.lower() not in _VIDEO:
        raise ValueError("Native phone posting currently supports videos/reels, not images, stories or carousels")
    if not path.is_file() or path.stat().st_size == 0:
        raise ValueError("The retained video is missing or empty")
    expected = str(hashes[0])
    if not re.fullmatch(r"[0-9a-f]{64}", expected) or _hash(path) != expected:
        raise ValueError("The approved video changed; create a new draft and approval")
    return platform, username, path, expected


def _nodes(session, package: str) -> list[dict]:
    if session.app_current().get("package") != package:
        raise RuntimeError("The expected native app is not in front")
    root = ET.fromstring(session.dump_hierarchy())
    return [dict(node.attrib) for node in root.iter("node")
            if node.get("package") in {None, "", package}]


def _label(node: dict) -> str:
    return (node.get("text") or node.get("content-desc") or "").strip()


def _click(session, nodes: list[dict]) -> bool:
    """Only click one unambiguous enabled target from the current UI dump."""
    candidates = [n for n in nodes if n.get("enabled", "true") == "true"]
    bounds = {n.get("bounds", "") for n in candidates}
    if len(bounds) != 1:
        return False
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", next(iter(bounds)))
    if match is None:
        return False
    left, top, right, bottom = map(int, match.groups())
    if right <= left or bottom <= top:
        return False
    session.click((left + right) // 2, (top + bottom) // 2)
    return True


def _own_profile(session, package: str, platform: str, username: str, sleep: Callable) -> None:
    nodes = _nodes(session, package)
    profile = [n for n in nodes if _label(n) == "Profile" or
               n.get("resource-id") == f"{package}:id/profile_tab"]
    if not _click(session, profile):
        raise RuntimeError("The Profile tab needs calibration; no account has been verified")
    sleep(1)
    nodes = _nodes(session, package)
    if not any(_label(n).lower() == "edit profile" for n in nodes):
        raise PermissionError("The phone did not show the signed-in account's own profile")
    if platform == "instagram":
        identity = [n for n in nodes if n.get("resource-id", "").rsplit("/", 1)[-1] in
                    {"action_bar_title", "profile_header_username", "username"}]
    else:
        # On TikTok the handle is displayed as @name on the own-profile screen.
        identity = [n for n in nodes if _label(n).startswith("@") and
                    n.get("class") != "android.widget.EditText"]
    handles = {_label(n).lstrip("@").lower() for n in identity if _label(n)}
    if handles != {username}:
        raise PermissionError("The active phone account does not exactly match the configured username")


def _media_uri(device, filename: str) -> str | None:
    # Generated filenames contain only [a-z0-9.-], so the SQL expression and ADB
    # arguments cannot contain caller-controlled shell/SQL syntax.
    query = device._adb("shell", "content", "query", "--uri", "content://media/external/video/media",
                        "--projection", "_id:_display_name", "--where", shlex.quote(f"_display_name='{filename}'"))
    if query.returncode != 0:
        return None
    matches = re.findall(r"(?:^|\n)Row: \d+ _id=(\d+), _display_name=([^\r\n]+)", query.stdout or "")
    if len(matches) != 1 or matches[0][1].strip() != filename or int(matches[0][0]) <= 0:
        return None
    return f"content://media/external/video/media/{matches[0][0]}"


def _caption_fields(nodes: list[dict]) -> list[dict]:
    hints = ("write a caption", "describe your post", "add description")
    names = {"caption_input_text_view", "caption_text_view", "caption_input", "description_input"}
    return [n for n in nodes if n.get("class") == "android.widget.EditText" and
            (n.get("resource-id", "").rsplit("/", 1)[-1] in names or
             any(hint in _label(n).lower() for hint in hints))]


def _prepare_composer(session, package: str, caption: str, sleep: Callable) -> list[dict]:
    for _ in range(4):
        nodes = _nodes(session, package)
        fields = _caption_fields(nodes)
        if len(fields) == 1 and _click(session, fields):
            session.send_keys(caption, clear=True)
            sleep(0.5)
            updated = _nodes(session, package)
            filled = [n for n in updated if n.get("bounds") == fields[0].get("bounds") and
                      n.get("class") == "android.widget.EditText"]
            if len(filled) != 1 or filled[0].get("text", "") != caption:
                raise RuntimeError("The native caption could not be verified exactly; finish the draft manually")
            return updated
        next_button = [n for n in nodes if _label(n) == "Next"]
        if not next_button:
            # Instagram can ask which destination receives an ACTION_SEND.
            # The supported kind is Reel, so never select Stories or Chats.
            next_button = [n for n in nodes if _label(n) in {"Reel", "Reels"}]
        if not _click(session, next_button):
            return nodes
        sleep(1)
    return _nodes(session, package)


def _screenshot(device, post_id, suffix: str) -> str | None:
    safe = hashlib.sha256(str(post_id).encode()).hexdigest()[:16]
    try:
        path = device.screenshot(f"post-{safe}-{suffix}")
        return str(path) if path is not None else None
    except Exception:
        return None


def publish(post: dict, account: dict, settings: dict, on_submit: Callable[[], None], *,
            sleep: Callable[[float], None] = time.sleep) -> dict:
    """Prepare one video; submit once using its exact Android media content URI.

    `on_submit` persists submission uncertainty before the final native click.
    Its exceptions deliberately propagate: a lost lease must prevent Submit.
    Screenshots are private controller files for the backend to copy to the chat.
    """
    platform, username, source, expected = _snapshot(post, account, settings)
    device = _device(settings)
    remote = None
    try:
        if not device.connect() or not device.is_connected():
            raise RuntimeError("The configured phone is not connected through ADB")
        session, package = device.session(), _PACKAGES[platform]
        session.app_start(package, use_monkey=True)
        sleep(1)
        _own_profile(session, package, platform, username, sleep)
        # Copy and rehash the bytes actually transferred, closing the race between
        # approved-file validation and an Android upload.
        with tempfile.TemporaryDirectory(prefix="hub-phone-video-") as temporary:
            job_key = hashlib.sha256(str(post["id"]).encode()).hexdigest()[:16]
            filename = f"hub-{job_key}-{expected[:16]}{source.suffix.lower()}"
            staged = Path(temporary) / filename
            shutil.copyfile(source, staged)
            if _hash(staged) != expected:
                raise ValueError("The approved video changed while preparing it")
            remote = device.push_file(staged)
        sleep(1)
        uri = None
        for _ in range(4):
            uri = _media_uri(device, filename)
            if uri is not None:
                break
            sleep(1)
        reason = "The exact transferred video could not be located in Android's media database."
        nodes = []
        if uri is not None:
            result = device._adb("shell", "am", "start", "-W", "-a", "android.intent.action.SEND",
                                 "-t", "video/quicktime" if source.suffix.lower() == ".mov" else "video/mp4",
                                 "-p", package, "--eu", "android.intent.extra.STREAM", uri,
                                 "-f", "1")
            if result.returncode != 0 or re.search(r"\b(?:Error|Exception):", (result.stdout or "") + (result.stderr or "")):
                reason = "The app could not open the exact video; choose it manually on the phone."
            else:
                sleep(1)
                nodes = _prepare_composer(session, package, str(post.get("caption") or ""), sleep)
                reason = "The app's composer or final caption could not be verified; review and publish manually."
        # OS routing chooses the exact validated MediaStore row, bypassing the
        # app's gallery. A composer with the exact caption and final button must
        # also be visible in the intended package before Submit is permitted.
        fields = [n for n in nodes if n.get("class") == "android.widget.EditText" and
                  n.get("text", "") == str(post.get("caption") or "")]
        buttons = [n for n in nodes if _label(n) == ("Share" if platform == "instagram" else "Post")]
        if uri is None or len(fields) != 1 or len({n.get("bounds") for n in buttons}) != 1:
            return {"ok": True, "status": "awaiting_manual_publish", "submitted": False,
                    "reason": reason, "remote_media": remote, "caption": str(post.get("caption") or ""),
                    "screenshot": _screenshot(device, post["id"], "prepared")}
        # Resolve and validate the final target before committing the submission
        # boundary; the following click is the sole permitted publication press.
        target = [n for n in buttons if n.get("enabled", "true") == "true"]
        if len({n.get("bounds") for n in target}) != 1 or not target:
            raise RuntimeError("The final publish button is not unambiguous and enabled")
        match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", target[0].get("bounds", ""))
        if match is None:
            raise RuntimeError("The publish button has no verified bounds")
        left, top, right, bottom = map(int, match.groups())
        if right <= left or bottom <= top:
            raise RuntimeError("The publish button has invalid bounds")
    except Exception as error:
        if remote is not None:
            # A prepared native draft must not be overwritten by the next job
            # just because its layout/input could not be verified automatically.
            return {"ok": True, "status": "awaiting_manual_publish", "submitted": False,
                    "reason": str(error)[:400], "remote_media": remote,
                    "caption": str(post.get("caption") or ""),
                    "screenshot": _screenshot(device, post["id"], "prepared")}
        return {"ok": False, "status": "failed", "submitted": False,
                "error": str(error)[:400], "screenshot": _screenshot(device, post.get("id"), "blocked")}

    on_submit()
    try:
        # Uiautomator's RPC wrapper retries an HTTP/connection failure. Use one
        # non-retrying ADB input command for the irreversible publication tap.
        tapped = device._adb("shell", "input", "tap", str((left + right) // 2), str((top + bottom) // 2))
        if tapped.returncode != 0:
            raise RuntimeError("The publication tap could not be acknowledged")
        sleep(2)
        reason = "Submit was pressed once. Confirm the resulting owned post link; no automatic retry will occur."
    except Exception:
        # A disconnected phone may already have accepted the click. Keep the
        # durable hold even when we cannot observe the result.
        reason = "The phone disconnected during Submit. Check the profile and confirm the resulting link before any new post."
    return {"ok": True, "status": "needs_confirmation", "submitted": True, "needs_confirmation": True,
            "reason": reason, "screenshot": _screenshot(device, post["id"], "submitted")}
