"""Local SQLite database of collected posts and generated drafts (agents/data/hub.db)."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"

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


def connect() -> sqlite3.Connection:
    DATA.mkdir(exist_ok=True)
    db = sqlite3.connect(DATA / "hub.db")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def count_text(value) -> int | None:
    """Turn '12.3K', '1.2M', '4,512' into integers."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    m = re.match(r"\s*([\d.,]+)\s*([KkMmBb]?)", str(value))
    if not m:
        return None
    n = float(m.group(1).replace(",", ""))
    return int(n * {"": 1, "k": 1e3, "m": 1e6, "b": 1e9}[m.group(2).lower()])


def fingerprint(platform: str, creator: str | None, caption: str | None, on_screen: str | None) -> str:
    basis = f"{platform}|{(creator or '').lower().strip()}|{(caption or on_screen or '').lower().strip()[:120]}"
    return hashlib.sha256(basis.encode()).hexdigest()


def save_post(db: sqlite3.Connection, platform: str, post: dict, screenshot: str) -> int | None:
    """Insert a post; returns its id, or None if it was already collected."""
    fp = fingerprint(platform, post.get("creator"), post.get("caption"), post.get("on_screen_text"))
    cur = db.execute(
        """INSERT OR IGNORE INTO posts (platform, fingerprint, creator, caption, on_screen_text, visual_summary, topic,
           hashtags, sound, likes, comments, shares, is_ad, screenshot, collected_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (platform, fp, post.get("creator"), post.get("caption"), post.get("on_screen_text"), post.get("visual_summary"),
         post.get("topic"), json.dumps(post.get("hashtags") or []), post.get("sound"),
         count_text(post.get("likes")), count_text(post.get("comments")), count_text(post.get("shares")),
         int(bool(post.get("is_ad"))), screenshot, now()),
    )
    db.commit()
    return cur.lastrowid if cur.rowcount else None
