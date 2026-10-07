"""Approved, durable native-phone posts, independent of a Cowork model run.

Only additive tables live in hub.db. Original drafts, media and project memories
stay where they are. The phone lock survives async cancellation until the actual
publisher thread exits; a crash releases it. The durable submit checkpoint is
written *before* the final native Publish press, so ambiguous work is never replayed.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
from urllib.parse import urlsplit, urlunsplit
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import store
import workspace as ws

log = logging.getLogger(__name__)
MAX_MEDIA = 200 * 1024 * 1024
MAX_ATTEMPTS = 3
HELD = {"needs_confirmation", "awaiting_manual_publish"}
ACTIVE = {"queued", "processing", *HELD}
CADENCE = {"window_start": "08:00", "window_end": "22:00", "minimum_gap_minutes": 20,
           "daily_caps": {"instagram": 5, "tiktok": 8}}
SCHEMA = """
CREATE TABLE IF NOT EXISTS social_posts (
 id INTEGER PRIMARY KEY, job_id TEXT, project TEXT NOT NULL, platform TEXT NOT NULL, kind TEXT,
 caption TEXT NOT NULL DEFAULT '', media TEXT NOT NULL DEFAULT '[]', status TEXT NOT NULL,
 result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_approvals (
 id INTEGER PRIMARY KEY, project TEXT NOT NULL, post_id INTEGER NOT NULL,
 job_id TEXT NOT NULL, run_at TEXT NOT NULL, snapshot TEXT NOT NULL,
 snapshot_hash TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS automation_queue (
 post_id INTEGER PRIMARY KEY, project TEXT NOT NULL, account_id TEXT NOT NULL,
 approval_id INTEGER NOT NULL REFERENCES automation_approvals(id), status TEXT NOT NULL,
 run_at TEXT NOT NULL, next_attempt_at TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
 claim_token TEXT, submitted_at TEXT, remote_id TEXT, url TEXT, published_at TEXT,
 result TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS automation_due ON automation_queue(status,next_attempt_at,run_at);
CREATE UNIQUE INDEX IF NOT EXISTS automation_publication ON automation_queue(account_id,remote_id)
 WHERE remote_id IS NOT NULL;
"""


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _time(value: str, label: str) -> datetime:
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError
        return result.astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(f"{label} must include a UTC offset, for example 2026-10-08T12:00:00-04:00") from None


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _folder() -> Path:
    return store.DATA / "automation"


def init() -> None:
    """Add only automation tables; safe on databases with existing drafts/memory."""
    with ws.connection() as db:
        db.executescript(SCHEMA)


def _config() -> dict:
    path = _folder() / "config.json"
    if not path.exists():
        return {"version": 1, "enabled": False, "device_serial": "", "accounts": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), dict):
            raise ValueError
        return data
    except (OSError, ValueError):
        raise ValueError("Phone configuration is unreadable; repair automation/config.json without resetting the data folder") from None


def _serial(value: str) -> str:
    try:
        parsed = urlsplit("//" + str(value).strip())
        if parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError
        host, port = parsed.hostname, parsed.port
        if not host or not port or not 1 <= port <= 65535:
            raise ValueError
        if host != "localhost":
            address = ipaddress.ip_address(host)
            tailnet = ipaddress.ip_network("100.64.0.0/10") if address.version == 4 else ipaddress.ip_network("fd7a:115c:a1e0::/48")
            if not address.is_loopback and address not in tailnet:
                raise ValueError
        return f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    except (ValueError, TypeError):
        raise ValueError("Use a Tailscale IP:port or localhost:port for the private SSH ADB tunnel") from None


def configure(device_serial: str, platform: str, username: str, timezone_name: str, country: str) -> dict:
    """Configure this one phone and one account per platform; no credentials or IP identity map."""
    init()
    serial = _serial(device_serial)
    if platform not in {"instagram", "tiktok"}:
        raise ValueError("Phone posting supports instagram or tiktok")
    handle = str(username).strip().lstrip("@").lower()
    if not re.fullmatch(r"[a-z0-9._]{1,30}", handle):
        raise ValueError("Use the account's actual username, without a URL")
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ValueError("Use a valid timezone such as America/New_York") from None
    country = str(country).strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise ValueError("Country must be a two-letter code such as US")
    account = {"id": platform, "platform": platform, "username": handle,
               "timezone_name": timezone_name, "country": country}
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        data = _config()
        pending = db.execute("SELECT account_id FROM automation_queue WHERE status IN ('queued','processing','needs_confirmation','awaiting_manual_publish')").fetchall()
        if pending and data.get("device_serial") != serial:
            raise ValueError("Resolve or cancel pending phone posts before changing the phone")
        if any(r[0] == platform for r in pending) and data["accounts"].get(platform) != account:
            raise ValueError("Resolve or cancel this account's pending posts before changing its identity")
        data.update(version=1, enabled=True, device_serial=serial)
        data.setdefault("cadence", CADENCE)
        data["accounts"][platform] = account
        folder = _folder()
        folder.mkdir(parents=True, exist_ok=True)
        temporary = folder / ("config-" + uuid.uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as handle_file:
                handle_file.write(_json(data))
                handle_file.flush()
                os.fsync(handle_file.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, folder / "config.json")
        finally:
            temporary.unlink(missing_ok=True)
    return {"enabled": True, "device_serial": serial, "account": account}


def configured_account(platform: str) -> dict | None:
    try:
        data = _config()
        return dict(data["accounts"][platform]) if data.get("enabled") and platform in data["accounts"] else None
    except ValueError:
        return None


def _readiness() -> dict:
    try:
        import automation_runtime
        return automation_runtime.ready()
    except ImportError:
        missing = []
        adb = shutil.which("adb")
        if not adb:
            missing.append("adb")
        if importlib.util.find_spec("uiautomator2") is None:
            missing.append("uiautomator2")
        return {"ready": not missing, "missing": missing, "adb": adb}
    except Exception as error:
        return {"ready": False, "missing": [f"phone runtime: {type(error).__name__}"]}


def _project(project: str) -> None:
    if not ws.project_exists(project):
        raise ValueError("Unknown project")


def _post_id(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("Post ID must be a positive integer")
    return value


def _draft(db, project: str, post_id: int) -> dict:
    row = db.execute("SELECT * FROM social_posts WHERE id=? AND project=?", (_post_id(post_id), project)).fetchone()
    if not row:
        raise ValueError("Draft not found in this project")
    result = dict(row)
    if result["platform"] not in {"instagram", "tiktok"}:
        raise ValueError("Native phone scheduling supports instagram or tiktok videos")
    if result["platform"] == "instagram" and result["kind"] != "reel":
        raise ValueError("Native Instagram scheduling currently supports a single video reel")
    try:
        result["media"] = json.loads(result["media"])
    except (ValueError, TypeError):
        raise ValueError("Draft media is unreadable") from None
    return result


def _hash_media(post: dict) -> list[str]:
    media = post["media"]
    if not isinstance(media, list) or len(media) != 1 or not isinstance(media[0], str):
        raise ValueError("Phone scheduling requires exactly one retained video")
    folder = (store.DATA / "posts" / str(post["id"])).resolve()
    path = Path(media[0]).resolve()
    if not path.is_relative_to(folder) or not path.is_file() or path.suffix.lower() not in {".mp4", ".mov"}:
        raise ValueError("The draft must contain a retained MP4 or MOV video in its own post folder")
    size = path.stat().st_size
    if not 12 <= size <= MAX_MEDIA:
        raise ValueError("The video is empty or exceeds 200 MB")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        first = handle.read(64)
        # Identify a supported container; native publishing verifies its usable video track.
        if first[4:8] not in {b"ftyp", b"moov", b"mdat", b"wide"}:
            raise ValueError("The retained file is not an MP4/MOV container")
        digest.update(first)
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return [digest.hexdigest()]


def _row(db, project: str, post_id: int) -> dict:
    row = db.execute("SELECT q.*,a.snapshot,a.snapshot_hash,a.job_id AS approved_by_job,a.created_at AS approved_at "
                     "FROM automation_queue q JOIN automation_approvals a ON a.id=q.approval_id "
                     "WHERE q.post_id=? AND q.project=?", (_post_id(post_id), project)).fetchone()
    if not row:
        raise ValueError("Scheduled post not found in this project")
    return dict(row)


def _view(row: dict) -> dict:
    result = {k: row.get(k) for k in ("post_id", "account_id", "status", "run_at", "next_attempt_at", "attempts",
                                    "submitted_at", "remote_id", "url", "published_at", "error", "approved_by_job",
                                    "approved_at", "snapshot_hash")}
    result["id"] = row["post_id"]
    if row.get("result"):
        result["result"] = json.loads(row["result"])
    return result


def _cadence_reason(db, account: dict, due: datetime, post_id: int, *, reservations: bool) -> str | None:
    """Deterministic operator limits; no invented engagement or randomized execution times."""
    policy = _config().get("cadence", CADENCE)
    local = due.astimezone(ZoneInfo(account["timezone_name"]))
    start = datetime.strptime(policy["window_start"], "%H:%M").time()
    end = datetime.strptime(policy["window_end"], "%H:%M").time()
    if not start <= local.time() < end:
        return f"Posting window is {start:%H:%M}–{end:%H:%M} in {account['timezone_name']}; approve a time inside that window"
    history = []
    for row in db.execute("SELECT q.*,a.snapshot FROM automation_queue q JOIN automation_approvals a ON a.id=q.approval_id "
                          "WHERE q.account_id=? AND q.post_id<>? AND q.status NOT IN ('failed','canceled','abandoned','blocked')",
                          (account["id"], post_id)):
        prior = json.loads(row["snapshot"])
        if prior["account"]["username"] != account["username"]:
            continue
        instant = row["published_at"] or row["submitted_at"] or (row["run_at"] if reservations else None)
        if instant:
            history.append(_time(instant, "Prior publication time"))
    gap = timedelta(minutes=float(policy["minimum_gap_minutes"]))
    if any(abs(due - instant) < gap for instant in history):
        return f"This account needs at least {policy['minimum_gap_minutes']} minutes between approved posts; choose a later time"
    count = sum(instant.astimezone(local.tzinfo).date() == local.date() for instant in history)
    cap = int(policy["daily_caps"][account["platform"]])
    if count >= cap:
        return f"This account already has {cap} approved posts on that local date; choose another date"
    return None


def status(project: str) -> dict:
    init()
    _project(project)
    error = None
    try:
        data = _config()
    except ValueError as exception:
        data = {"enabled": False, "device_serial": "", "accounts": {}}
        error = str(exception)
    with ws.connection() as db:
        rows = db.execute("SELECT q.*,a.snapshot_hash,a.job_id AS approved_by_job,a.created_at AS approved_at "
                          "FROM automation_queue q JOIN automation_approvals a ON a.id=q.approval_id "
                          "WHERE q.project=? ORDER BY q.updated_at DESC,q.post_id DESC LIMIT 30", (project,)).fetchall()
        blocker = db.execute("SELECT post_id,project FROM automation_queue WHERE status IN ('needs_confirmation','awaiting_manual_publish') LIMIT 1").fetchone()
    return {"enabled": bool(data.get("enabled")), "device_serial": data.get("device_serial", ""),
            "accounts": list(data["accounts"].values()), "readiness": _readiness(), "error": error,
            "cadence": data.get("cadence", CADENCE), "blocked": bool(blocker),
            "blocking_post_id": blocker["post_id"] if blocker and blocker["project"] == project else None,
            "posts": [_view(dict(row)) for row in rows]}


def schedule(project: str, post_id: int, account_id: str, run_at: str, approved_by_job: str) -> dict:
    """The caller must verify the user's current explicit approval before calling.

    Approval binds the exact draft, video bytes, account, phone and UTC execution
    time. A retry cannot substitute new content or a different account.
    """
    init()
    _project(project)
    due = _time(run_at, "Schedule time")
    if due < _clock() - timedelta(minutes=5) or due > _clock() + timedelta(days=366):
        raise ValueError("Schedule time must be now or within the next year")
    if not isinstance(approved_by_job, str) or not approved_by_job:
        raise ValueError("Record the Cowork job that received this draft's approval")
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        if not db.execute("SELECT 1 FROM jobs WHERE id=? AND project=?", (approved_by_job, project)).fetchone():
            raise ValueError("Approval job does not belong to this project")
        data = _config()
        account = data["accounts"].get(account_id)
        if not data.get("enabled") or not account:
            raise ValueError("Configure the phone and its account before scheduling")
        post = _draft(db, project, post_id)
        if post["platform"] != account["platform"]:
            raise ValueError("Draft platform differs from the configured account")
        if len(post["caption"]) > (2200 if post["platform"] == "instagram" else 4000):
            raise ValueError("Caption exceeds this platform's native posting limit")
        exact_post = {k: post[k] for k in ("id", "project", "platform", "kind", "caption", "media")}
        exact_post["media_hashes"] = _hash_media(exact_post)
        snapshot = {"post": exact_post, "account": account, "device_serial": data["device_serial"],
                    "run_at": _iso(due), "approved_by_job": approved_by_job}
        encoded = _json(snapshot)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        existing = db.execute("SELECT 1 FROM automation_queue WHERE post_id=?", (post_id,)).fetchone()
        if existing:
            old = _row(db, project, post_id)
            if old["status"] == "queued" and old["snapshot_hash"] == digest:
                return _view(old)
            if old["status"] in ACTIVE or old["status"] in {"published", "abandoned"} or old["submitted_at"]:
                raise ValueError("This post is already scheduled, published or awaiting confirmation; do not publish it again")
        if post["status"] not in {"draft", "failed", "canceled", "blocked"}:
            raise ValueError("Only an unpublished draft can be scheduled")
        if reason := _cadence_reason(db, account, due, post_id, reservations=True):
            raise ValueError(reason)
        now = _iso(_clock())
        approval = db.execute("INSERT INTO automation_approvals(project,post_id,job_id,run_at,snapshot,snapshot_hash,created_at) "
                              "VALUES (?,?,?,?,?,?,?)", (project, post_id, approved_by_job, _iso(due), encoded, digest, now)).lastrowid
        db.execute("INSERT INTO automation_queue(post_id,project,account_id,approval_id,status,run_at,next_attempt_at,created_at,updated_at) "
                   "VALUES (?,?,?,?,'queued',?,?,?,?) ON CONFLICT(post_id) DO UPDATE SET approval_id=excluded.approval_id,"
                   "account_id=excluded.account_id,status='queued',run_at=excluded.run_at,next_attempt_at=excluded.next_attempt_at,"
                   "attempts=0,claim_token=NULL,result=NULL,error=NULL,updated_at=excluded.updated_at",
                   (post_id, project, account_id, approval, _iso(due), _iso(due), now, now))
        db.execute("UPDATE social_posts SET status='scheduled',updated_at=? WHERE id=? AND project=?", (now, post_id, project))
        return _view(_row(db, project, post_id))


def cancel(project: str, post_id: int, verified_not_published: bool = False) -> dict:
    init()
    _project(project)
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, project, post_id)
        if row["status"] == "processing":
            with _phone_lock() as acquired:
                if not acquired:
                    raise ValueError("The native publisher is already preparing this post; wait for its result before canceling")
        if row["status"] == "published":
            raise ValueError("A confirmed publication cannot be canceled by this queue")
        held = row["status"] in HELD or bool(row["submitted_at"])
        if held and verified_not_published is not True:
            raise ValueError("Check the phone and discard its composer first; explicitly confirm this post was not published to release the hold")
        if held:
            with _phone_lock() as acquired:
                if not acquired:
                    raise ValueError("The native publisher is still active; wait for it to finish before recording that nothing was published")
        target = "abandoned" if held or row["status"] == "abandoned" else "canceled"
        receipt = "Owner verified not published and discarded the native composer; this draft must never be replayed" if target == "abandoned" else "Canceled by the owner"
        result = json.loads(row["result"] or "{}")
        result.update(status=target, reason=receipt, needs_confirmation=False, owner_verified_not_published=target == "abandoned",
                      verification_recorded_at=_iso(_clock()))
        db.execute("UPDATE automation_queue SET status=?,claim_token=NULL,result=?,error=?,updated_at=? WHERE post_id=? AND project=?",
                   (target, _json(result), receipt, _iso(_clock()), post_id, project))
        db.execute("UPDATE social_posts SET status=?,result=?,updated_at=? WHERE id=? AND project=?",
                   (target, _json(result), _iso(_clock()), post_id, project))
        return _view(_row(db, project, post_id))


def _identity(snapshot: dict, remote_id: str, url: str, published_at: str) -> dict:
    platform, account = snapshot["post"]["platform"], snapshot["account"]
    if not isinstance(remote_id, str) or not isinstance(url, str) or len(url) > 2000:
        raise ValueError("Supply the actual published post ID and direct HTTPS permalink")
    parsed = urlsplit(url)
    hosts = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"} if platform == "tiktok" else {"instagram.com", "www.instagram.com"}
    if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password or parsed.port:
        raise ValueError("Use a direct HTTPS post URL on this draft's platform")
    if platform == "tiktok":
        match = re.fullmatch(r"/@([^/]+)/video/([0-9]{1,40})/?", parsed.path)
        if not match or match[2] != remote_id or match[1].lower() != account["username"]:
            raise ValueError("TikTok permalink must match this account and published video ID")
    else:
        match = re.fullmatch(r"/(?:reel|p)/([A-Za-z0-9_-]{1,80})/?", parsed.path)
        if not match or match[1] != remote_id:
            raise ValueError("For a native Instagram post, use its permalink shortcode as the post ID")
    published = _time(published_at, "Publication time")
    if published.year < 2000 or published > _clock() + timedelta(minutes=5):
        raise ValueError("Publication time must describe an existing post")
    return {"id": remote_id, "url": urlunsplit(("https", parsed.hostname, parsed.path.rstrip("/"), "", "")),
            "published_at": _iso(published), "account": account["username"]}


def _record_publication(db, row: dict, identity: dict, confirmation: str) -> None:
    result = json.loads(row["result"] or "{}")
    previous = {key: result.pop(key) for key in ("reason", "error") if key in result}
    if previous:
        result["submission_receipt"] = previous
    result.update(identity, ok=True, status="published", needs_confirmation=False, confirmation=confirmation)
    try:
        db.execute("UPDATE automation_queue SET status='published',claim_token=NULL,remote_id=?,url=?,published_at=?,result=?,error=NULL,updated_at=? "
                   "WHERE post_id=? AND project=?", (identity["id"], identity["url"], identity["published_at"], _json(result),
                                                     _iso(_clock()), row["post_id"], row["project"]))
    except sqlite3.IntegrityError:
        raise ValueError("This published post is already recorded for another draft") from None
    db.execute("UPDATE social_posts SET status='published',result=?,updated_at=? WHERE id=? AND project=?",
               (_json(result), _iso(_clock()), row["post_id"], row["project"]))


def confirm(project: str, post_id: int, remote_id: str, url: str, published_at: str) -> dict:
    """Record an owner-supplied publication; never infer success from a vanished banner."""
    init()
    _project(project)
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        row = _row(db, project, post_id)
        identity = _identity(json.loads(row["snapshot"]), remote_id, url, published_at)
        if row["status"] == "published":
            if (row["remote_id"], row["url"], row["published_at"]) != (identity["id"], identity["url"], identity["published_at"]):
                raise ValueError("A confirmed publication cannot be reassigned")
        else:
            if row["status"] not in HELD:
                raise ValueError("Only a submitted or manually prepared post can be reconciled")
            if _time(identity["published_at"], "Publication time") < _time(row["approved_at"], "Approval time") - timedelta(minutes=5):
                raise ValueError("The publication predates this draft's approval")
            _record_publication(db, row, identity, "owner_recorded")
    _link_audience(project, post_id)
    with ws.connection() as db:
        return _view(_row(db, project, post_id))


def _link_audience(project: str, post_id: int) -> None:
    """Continue existing experiment tracking only when its identity rules accept the evidence."""
    with ws.connection() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='audience_variants'").fetchone():
            return
        row = _row(db, project, post_id)
    if row["status"] != "published":
        return
    import audience
    variant = audience.variant_for_post(project, post_id)
    if variant is None:
        return
    snapshot = json.loads(row["snapshot"])
    try:
        audience.confirm_publication(project, variant["id"], row["remote_id"], row["url"], row["published_at"], snapshot["account"]["username"])
        warning = None
    except ValueError as error:
        warning = f"Publication is recorded; audience tracking needs reconciliation: {error}"
    with ws.connection() as db, db:
        current = _row(db, project, post_id)
        result = json.loads(current["result"] or "{}")
        if warning:
            result["audience_warning"] = warning
        else:
            result.pop("audience_warning", None)
            result["audience_variant_id"] = variant["id"]
        db.execute("UPDATE automation_queue SET result=? WHERE post_id=? AND project=?", (_json(result), post_id, project))
        db.execute("UPDATE social_posts SET result=? WHERE id=? AND project=?", (_json(result), post_id, project))


@contextmanager
def _phone_lock():
    folder = _folder()
    folder.mkdir(parents=True, exist_ok=True)
    handle = (folder / "phone.lock").open("a+b")
    acquired = False
    try:
        try:
            if os.name == "nt":
                import msvcrt
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        handle.close()


def _recover(db, now: str) -> None:
    # Only call under the phone lock: no publisher can still be preparing these rows.
    for row in db.execute("SELECT * FROM automation_queue WHERE status='processing'").fetchall():
        target = "needs_confirmation" if row["submitted_at"] else "failed" if row["attempts"] >= MAX_ATTEMPTS else "queued"
        reason = "Controller stopped after possible submission; confirm the actual post" if row["submitted_at"] else "Controller stopped before submission"
        db.execute("UPDATE automation_queue SET status=?,claim_token=NULL,error=?,next_attempt_at=?,updated_at=? WHERE post_id=?",
                   (target, reason, now, now, row["post_id"]))
        db.execute("UPDATE social_posts SET status=?,updated_at=? WHERE id=? AND project=?",
                   ("scheduled" if target == "queued" else target, now, row["post_id"], row["project"]))


def _validate_snapshot(db, row: dict) -> dict:
    snapshot = json.loads(row["snapshot"])
    if hashlib.sha256(row["snapshot"].encode()).hexdigest() != row["snapshot_hash"]:
        raise ValueError("The saved approval snapshot is damaged")
    post = _draft(db, row["project"], row["post_id"])
    expected = snapshot["post"]
    if any(post[key] != expected[key] for key in ("id", "project", "platform", "kind", "caption", "media")):
        raise ValueError("The draft changed after approval; approve the exact revised draft again")
    if _hash_media(expected) != expected["media_hashes"]:
        raise ValueError("The video changed after approval; approve the exact revised draft again")
    data = _config()
    if not data.get("enabled") or data.get("device_serial") != snapshot["device_serial"] or data["accounts"].get(row["account_id"]) != snapshot["account"]:
        raise ValueError("The configured phone/account differs from the approved snapshot")
    return snapshot


def _submit(row: dict) -> None:
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        current = _row(db, row["project"], row["post_id"])
        if current["claim_token"] != row["claim_token"] or current["status"] != "processing":
            raise PermissionError("Posting was canceled or this worker lost its claim before submission")
        snapshot = _validate_snapshot(db, current)
        if reason := _cadence_reason(db, snapshot["account"], _clock(), row["post_id"], reservations=False):
            raise PermissionError(reason)
        now = _iso(_clock())
        result = {"ok": False, "status": "needs_confirmation", "submitted": True,
                  "reason": "Submission may occur now; verify the actual post before trying anything again"}
        db.execute("UPDATE automation_queue SET status='needs_confirmation',submitted_at=?,result=?,updated_at=? WHERE post_id=?",
                   (now, _json(result), now, row["post_id"]))
        db.execute("UPDATE social_posts SET status='needs_confirmation',result=?,updated_at=? WHERE id=? AND project=?",
                   (_json(result), now, row["post_id"], row["project"]))


def _safe_result(row: dict, result: dict) -> dict:
    allowed = {"ok", "status", "submitted", "needs_confirmation", "reason", "error", "caption", "remote_media", "id", "url", "published_at", "retryable"}
    clean = {key: value for key, value in result.items() if key in allowed}
    for key, value in clean.items():
        if isinstance(value, str):
            clean[key] = value.replace(str(store.DATA.resolve()), "[persistent data]")
    if result.get("screenshot"):
        try:
            path = Path(result["screenshot"]).resolve()
            if path.is_relative_to((_folder() / "screenshots").resolve()) and path.is_file() and path.stat().st_size <= 20 * 1024 * 1024:
                artifact = ws.write_artifact(row["project"], row["approved_by_job"], f"post-{row['post_id']}-verification.png", path.read_bytes(), "image/png")
                clean["verification_artifact"] = {**artifact, "url": f"/api/artifacts/{artifact['id']}"}
        except Exception:
            clean["verification_warning"] = "A verification screenshot could not be retained"
    return clean


def _publisher(post: dict, account: dict, settings: dict, on_submit) -> dict:
    import automation_publisher
    return automation_publisher.publish(post, account, settings, on_submit)


def _finish(row: dict, outcome: dict, snapshot: dict | None) -> dict:
    with ws.connection() as db, db:
        db.execute("BEGIN IMMEDIATE")
        current = _row(db, row["project"], row["post_id"])
        if current["claim_token"] != row["claim_token"] or current["status"] == "published":
            return _view(current)
        if current["submitted_at"] and outcome.get("status") == "published":
            try:
                identity = _identity(snapshot, outcome.get("id"), outcome.get("url"), outcome.get("published_at"))
                current["result"] = _json(outcome)
                _record_publication(db, current, identity, "native_verified")
                return _view(_row(db, row["project"], row["post_id"]))
            except ValueError as error:
                outcome["reason"] = f"Publication evidence needs owner confirmation: {error}"
        possible = bool(current["submitted_at"]) or outcome.get("submitted") is True or outcome.get("status") in {"published", "needs_confirmation"}
        target = "needs_confirmation" if possible else "awaiting_manual_publish" if outcome.get("status") == "awaiting_manual_publish" else "failed"
        if target == "failed" and outcome.get("retryable") is True and current["attempts"] < MAX_ATTEMPTS:
            target = "queued"
        reason = str(outcome.get("reason") or outcome.get("error") or "Publisher did not return verified publication evidence")[:1200]
        outcome["status"] = target
        next_attempt = _iso(_clock() + timedelta(seconds=60 * 2 ** (current["attempts"] - 1))) if target == "queued" else current["next_attempt_at"]
        submitted = current["submitted_at"] or (_iso(_clock()) if possible else None)
        db.execute("UPDATE automation_queue SET status=?,claim_token=NULL,submitted_at=?,result=?,error=?,next_attempt_at=?,updated_at=? WHERE post_id=? AND project=?",
                   (target, submitted, _json(outcome), reason, next_attempt, _iso(_clock()), row["post_id"], row["project"]))
        db.execute("UPDATE social_posts SET status=?,result=?,updated_at=? WHERE id=? AND project=?",
                   ("scheduled" if target == "queued" else target, _json(outcome), _iso(_clock()), row["post_id"], row["project"]))
        return _view(_row(db, row["project"], row["post_id"]))


def run_once() -> dict | None:
    """Claim and run at most one due post. Safe across threads and controller restarts."""
    init()
    with _phone_lock() as acquired:
        if not acquired:
            return {"status": "busy"}
        now = _iso(_clock())
        with ws.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            _recover(db, now)
        try:
            data = _config()
        except ValueError as error:
            return {"status": "disabled", "error": str(error)}
        if not data.get("enabled"):
            return None
        ready = _readiness()
        if not ready.get("ready"):
            return {"status": "blocked", "error": "Phone runtime is unavailable", "readiness": ready}
        with ws.connection() as db, db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute("SELECT project,post_id FROM automation_queue WHERE status='queued' AND run_at<=? AND next_attempt_at<=? "
                                 "ORDER BY run_at,post_id LIMIT 1", (now, now)).fetchone()
            if db.execute("SELECT 1 FROM automation_queue WHERE status IN ('needs_confirmation','awaiting_manual_publish') LIMIT 1").fetchone():
                return {"status": "blocked", "error": "The phone has a post awaiting owner confirmation or a manual composer; reconcile or explicitly abandon it first"}
            if not pending:
                return None
            pending_row = _row(db, pending["project"], pending["post_id"])
            try:
                account = json.loads(pending_row["snapshot"])["account"]
                reason = _cadence_reason(db, account, _clock(), pending["post_id"], reservations=False)
            except (ValueError, KeyError, TypeError) as error:
                reason = f"Posting policy needs review: {error}"
            if reason:
                db.execute("UPDATE automation_queue SET status='blocked',error=?,updated_at=? WHERE post_id=?", (reason, now, pending["post_id"]))
                db.execute("UPDATE social_posts SET status='blocked',updated_at=? WHERE id=? AND project=?", (now, pending["post_id"], pending["project"]))
                return _view(_row(db, pending["project"], pending["post_id"]))
            token = uuid.uuid4().hex
            db.execute("UPDATE automation_queue SET status='processing',attempts=attempts+1,claim_token=?,updated_at=? WHERE post_id=? AND status='queued'",
                       (token, now, pending["post_id"]))
            db.execute("UPDATE social_posts SET status='processing',updated_at=? WHERE id=? AND project=?", (now, pending["post_id"], pending["project"]))
            row = _row(db, pending["project"], pending["post_id"])
        snapshot = None
        prepared = False
        def on_submit():
            nonlocal prepared
            prepared = True  # Publisher has reached the final native composer.
            _submit(row)
        try:
            with ws.connection() as db:
                snapshot = _validate_snapshot(db, row)
            settings = {"enabled": True, "dry_run": False, "transport": "direct_adb", "device_serial": snapshot["device_serial"],
                        "adb_bin": ready.get("adb") or "adb", "screenshots_dir": str(_folder() / "screenshots")}
            post_input = {**snapshot["post"], "kind": snapshot["post"]["kind"] or "video"}
            outcome = _publisher(post_input, snapshot["account"], settings, on_submit)
            if not isinstance(outcome, dict):
                raise ValueError("Phone publisher returned an unreadable result")
        except Exception as error:
            outcome = {"ok": False, "error": f"{type(error).__name__}: {error}"[:1200],
                       "retryable": isinstance(error, (TimeoutError, ConnectionError))}
            if prepared:
                # A rejected boundary may leave a native composer open. Do not let
                # the next queued job overwrite it, even though no press was allowed.
                outcome.update(status="awaiting_manual_publish", retryable=False)
        outcome = _safe_result(row, outcome)
        finished = _finish(row, outcome, snapshot)
        if finished["status"] == "published":
            _link_audience(row["project"], row["post_id"])
            with ws.connection() as db:
                finished = _view(_row(db, row["project"], row["post_id"]))
        return finished


async def serve(stop: asyncio.Event | None = None) -> None:
    """Keep scheduled work running outside chat budgets. Missing phone dependencies are idle."""
    while stop is None or not stop.is_set():
        try:
            await asyncio.to_thread(run_once)
        except asyncio.CancelledError:
            # The thread retains the phone lock until it exits, including a final submit checkpoint.
            raise
        except Exception:
            log.exception("Phone scheduler iteration failed; saved approvals remain intact")
        if stop is None:
            await asyncio.sleep(2)
        else:
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass
