"""Per-account runtime state (JSON files under <state_dir>/runtime/). Identity mapping lives in accounts.yaml;
this holds what changes at runtime: posts, health, pauses, run log. `persist=False` keeps everything in memory
(dry-run never writes)."""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

UTC = timezone.utc
MAX_LOG = 200


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


@dataclass
class AccountState:
    account_id: str
    warmup_start: str                       # local date of warm-up day 1 (YYYY-MM-DD)
    health: str = "unknown"
    last_post_at: str | None = None
    post_log: list[dict[str, Any]] = field(default_factory=list)   # {ts, job_id, ok, detail}
    last_hashtags: list[str] = field(default_factory=list)
    run_log: list[dict[str, Any]] = field(default_factory=list)    # {ts, event, detail}
    consecutive_failures: int = 0
    paused_until: str | None = None
    pause_reason: str = ""
    last_watch_at: str | None = None
    shadowban_status: str = "unknown"       # last watcher verdict

    # -- derived
    def warmup_day(self, local_date: date) -> int:
        return max(1, (local_date - date.fromisoformat(self.warmup_start)).days + 1)

    def post_times(self) -> list[datetime]:
        return [datetime.fromisoformat(p["ts"]) for p in self.post_log if p.get("ok")]

    def is_paused(self, now: datetime) -> bool:
        pu = parse_iso(self.paused_until)
        return bool(pu and now < pu)

    # -- mutation
    def log(self, now: datetime, event: str, detail: str = "") -> None:
        self.run_log.append({"ts": iso(now), "event": event, "detail": detail})
        del self.run_log[:-MAX_LOG]

    def pause(self, now: datetime, days: float, reason: str) -> None:
        self.paused_until = iso(now + timedelta(days=days))
        self.pause_reason = reason
        self.log(now, "pause", f"{days}d: {reason}")

    def resume(self, now: datetime) -> None:
        self.paused_until, self.pause_reason = None, ""
        self.log(now, "resume")

    def record_post(self, now: datetime, job_id: str, ok: bool, detail: str, hashtags: list[str] | None = None) -> None:
        self.post_log.append({"ts": iso(now), "job_id": job_id, "ok": ok, "detail": detail})
        del self.post_log[:-MAX_LOG]
        if ok:
            self.last_post_at = iso(now)
            self.consecutive_failures = 0
            if hashtags:
                self.last_hashtags = list(hashtags)
        else:
            self.consecutive_failures += 1
        self.log(now, "post_ok" if ok else "post_fail", f"{job_id} {detail}".strip())


class StateStore:
    def __init__(self, directory: Path, persist: bool = True):
        self.dir = Path(directory)
        self.persist = persist
        self._mem: dict[str, AccountState] = {}
        self._jobs: dict[str, dict[str, str]] | None = None

    def _path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def _write(self, path: Path, data: Any) -> None:
        if not self.persist:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)  # atomic

    def get(self, account_id: str, today_local: date, warmup_day: int = 1, health: str = "unknown") -> AccountState:
        """Load (or create) state. `warmup_day` seeds warm-up day 1 = today - (warmup_day-1) on first creation."""
        if account_id in self._mem:
            return self._mem[account_id]
        p = self._path(account_id)
        if p.exists():
            st = AccountState(**json.loads(p.read_text()))
        else:
            start = today_local - timedelta(days=max(1, warmup_day) - 1)
            st = AccountState(account_id=account_id, warmup_start=start.isoformat(), health=health)
        self._mem[account_id] = st
        return st

    def save(self, st: AccountState) -> None:
        self._mem[st.account_id] = st
        self._write(self._path(st.account_id), asdict(st))

    # job completion ledger
    def _jobs_map(self) -> dict[str, dict[str, str]]:
        if self._jobs is None:
            p = self._path("_jobs")
            self._jobs = json.loads(p.read_text()) if p.exists() else {}
        return self._jobs

    def job_done(self, job_id: str) -> bool:
        return self._jobs_map().get(job_id, {}).get("status") == "done"

    def mark_job(self, job_id: str, status: str, now: datetime) -> None:
        self._jobs_map()[job_id] = {"status": status, "ts": iso(now)}
        self._write(self._path("_jobs"), self._jobs_map())
