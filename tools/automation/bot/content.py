"""Content pipeline: playbook jobs file -> validated PostJob objects."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import Account, ConfigError, Platform, Validation, resolve_with_example

HASHTAG_LIMITS = {Platform.IG: (3, 5), Platform.TIKTOK: (1, 5)}  # few + active; big tag blocks are a bot tell
CAPTION_MAX = 2200
VIDEO_EXT = {".mp4", ".mov", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png"}


@dataclass
class PostJob:
    job_id: str
    account_id: str
    platform: Platform
    asset_path: Path
    caption: str
    hashtags: list[str]
    post_time: datetime | None = None          # aware; "not before"
    platform_tags: dict[str, Any] = field(default_factory=dict)

    @property
    def media_kind(self) -> str:
        return "video" if self.asset_path.suffix.lower() in VIDEO_EXT else "image"

    def full_caption(self) -> str:
        tags = " ".join(f"#{h}" for h in self.hashtags)
        return f"{self.caption}\n\n{tags}".strip()


class ContentPipeline:
    def __init__(self, jobs_file: str | Path, accounts: dict[str, Account], allow_missing_assets: bool = False):
        self.path = resolve_with_example(Path(jobs_file))
        self.accounts = accounts
        self.allow_missing_assets = allow_missing_assets

    def load(self) -> tuple[list[PostJob], Validation]:
        v = Validation()
        try:
            raw = yaml.safe_load(self.path.read_text()) or {}
        except yaml.YAMLError as e:
            raise ConfigError([f"{self.path}: invalid YAML: {e}"]) from e
        items = raw.get("jobs")
        if not isinstance(items, list):
            raise ConfigError([f"{self.path.name}: 'jobs' must be a list"])
        jobs: list[PostJob] = []
        seen: set[str] = set()
        for i, r in enumerate(items):
            job = self._one(r, i, seen, v)
            if job:
                jobs.append(job)
        return jobs, v

    def _one(self, r: dict[str, Any], i: int, seen: set[str], v: Validation) -> PostJob | None:
        jid = str(r.get("id") or f"job{i}")
        w = f"job {jid}"
        n_err = len(v.errors)
        if jid in seen:
            v.errors.append(f"{w}: duplicate job id")
        seen.add(jid)
        acc = self.accounts.get(str(r.get("account")))
        if acc is None:
            v.errors.append(f"{w}: unknown account {r.get('account')!r}")
            return None
        try:
            plat = Platform(str(r.get("platform", acc.platform.value)).lower())
        except ValueError:
            v.errors.append(f"{w}: bad platform {r.get('platform')!r}")
            return None
        if plat != acc.platform:
            v.errors.append(f"{w}: platform {plat.value} != account {acc.id} platform {acc.platform.value}")
        tags = [str(t).lstrip("#") for t in r.get("hashtags") or []]
        lo, hi = HASHTAG_LIMITS[plat]
        if not lo <= len(tags) <= hi:
            v.errors.append(f"{w}: {plat.value} needs {lo}-{hi} hashtags, got {len(tags)}")
        caption = str(r.get("caption") or "")
        if len(caption) > CAPTION_MAX:
            v.errors.append(f"{w}: caption > {CAPTION_MAX} chars")
        asset_raw = r.get("asset_path")
        if not asset_raw:
            v.errors.append(f"{w}: asset_path missing")
            return None
        asset = (self.path.parent / asset_raw).resolve() if not Path(asset_raw).is_absolute() else Path(asset_raw)
        if asset.suffix.lower() not in VIDEO_EXT | IMAGE_EXT:
            v.errors.append(f"{w}: unsupported asset type {asset.suffix}")
        if not asset.exists():
            msg = f"{w}: asset not found: {asset}"
            (v.warnings if self.allow_missing_assets else v.errors).append(msg)
        if plat is Platform.TIKTOK and asset.suffix.lower() in IMAGE_EXT:
            v.warnings.append(f"{w}: TikTok image posts (photo mode) are not implemented in the driver")
        pt = r.get("post_time")
        post_time = None
        if pt:
            try:
                post_time = pt if isinstance(pt, datetime) else datetime.fromisoformat(str(pt))
                if post_time.tzinfo is None:
                    post_time = post_time.replace(tzinfo=acc.tz)  # naive == account-local
            except ValueError:
                v.errors.append(f"{w}: bad post_time {pt!r}")
        if len(v.errors) > n_err:
            return None
        return PostJob(jid, acc.id, plat, asset, caption, tags, post_time, dict(r.get("platform_tags") or {}))
