"""Local SQLite database of collected posts and generated drafts (agents/data/hub.db)."""
from __future__ import annotations

import hashlib
import json
import os
import math
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

DATA = Path(os.environ.get("HUB_DATA_DIR", Path(__file__).resolve().parent / "data"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL,
  fingerprint TEXT NOT NULL UNIQUE,
  creator TEXT, caption TEXT, on_screen_text TEXT, visual_summary TEXT,
  topic TEXT, hashtags TEXT, sound TEXT,
  likes INTEGER, comments INTEGER, shares INTEGER,
  is_ad INTEGER DEFAULT 0,
  screenshot TEXT, collected_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS posts_platform_time ON posts(platform, collected_at);
CREATE TABLE IF NOT EXISTS drafts (
  id INTEGER PRIMARY KEY,
  platform TEXT NOT NULL, title TEXT NOT NULL, hook TEXT, script TEXT, caption TEXT, hashtags TEXT,
  source_post_ids TEXT, notes TEXT, status TEXT DEFAULT 'draft', created_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_MIGRATED: set[str] = set()  # database files already migrated in this process


def connect() -> sqlite3.Connection:
    DATA.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA / "hub.db", timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA foreign_keys=ON")
    if str(DATA / "hub.db") in _MIGRATED:
        return db
    db.executescript(SCHEMA)
    # Additive migration, once per process: existing v2 data remains readable.
    with db:
        db.execute("BEGIN IMMEDIATE")
        for table, additions in {
            "posts": {"source_url": "TEXT", "source_id": "TEXT", "capture_hash": "TEXT", "device_serial": "TEXT", "project": "TEXT NOT NULL DEFAULT 'default'", "frames": "TEXT"},
            "drafts": {"project": "TEXT NOT NULL DEFAULT 'default'", "job_id": "TEXT"},
        }.items():
            present = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
            for name, declaration in additions.items():
                if name not in present:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
    _MIGRATED.add(str(DATA / "hub.db"))
    return db


def count_text(value) -> int | None:
    """Turn '12.3K', '1.2M', '4,512' into integers."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if math.isfinite(value) and value >= 0 else None
    m = re.match(r"\s*([\d.,]+)\s*([KkMmBb]?)", str(value))
    if not m:
        return None
    try:
        n = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    return int(n * {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2).lower()])


def fingerprint(platform: str, post: dict, project: str = "default") -> str:
    # Captions/handles are not identities. Prefer a supplied platform ID, then an exact
    # capture digest. Without either, keep the observation instead of silently losing it.
    identity = post.get("source_id") or post.get("source_url") or post.get("capture_hash") or uuid.uuid4().hex
    return hashlib.sha256(json.dumps(["v3", project, platform, identity]).encode()).hexdigest()


def save_post(db: sqlite3.Connection, platform: str, post: dict, screenshot: str, project: str = "default") -> int | None:
    """Insert a post; returns its id, or None if it was already collected."""
    if not post.get("capture_hash") and screenshot and Path(screenshot).is_file():
        post = {**post, "capture_hash": hashlib.sha256(Path(screenshot).read_bytes()).hexdigest()}
    creator, caption = (post.get("creator") or "").strip().lower(), (post.get("caption") or "").strip().lower()[:120]
    if creator and caption:
        # Video frames never hash the same twice, so a failed swipe would otherwise save the
        # same post again. Treat the same creator + caption on this device within 10 minutes as a repeat.
        recent = db.execute(
            """SELECT 1 FROM posts WHERE platform=? AND project=? AND IFNULL(device_serial,'')=?
               AND lower(trim(IFNULL(creator,'')))=? AND substr(lower(trim(IFNULL(caption,''))),1,120)=?
               AND collected_at >= ? LIMIT 1""",
            (platform, project, post.get("device_serial") or "", creator, caption,
             (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds"))).fetchone()
        if recent:
            return None
    fp = fingerprint(platform, post, project)
    cur = db.execute(
        """INSERT OR IGNORE INTO posts (platform, fingerprint, creator, caption, on_screen_text, visual_summary, topic,
           hashtags, sound, likes, comments, shares, is_ad, screenshot, collected_at,
           source_url, source_id, capture_hash, device_serial, project, frames)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (platform, fp, post.get("creator"), post.get("caption"), post.get("on_screen_text"), post.get("visual_summary"),
         post.get("topic"), json.dumps(post.get("hashtags") or []), post.get("sound"),
         count_text(post.get("likes")), count_text(post.get("comments")), count_text(post.get("shares")),
         int(bool(post.get("is_ad"))), screenshot, now(), post.get("source_url"), post.get("source_id"),
         post.get("capture_hash"), post.get("device_serial"), project, json.dumps(post.get("frames", []))),
    )
    db.commit()
    return cur.lastrowid if cur.rowcount else None
