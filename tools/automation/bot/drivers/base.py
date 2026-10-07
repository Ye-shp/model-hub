"""BaseDriver: step runner with DRY-RUN support + human-ish UI helpers (live only).

Every driver describes its flow as a list of (name, fn(d)) steps. In dry-run the steps are only logged, so the
dry-run path never touches (or depends on) any UI selector.
"""
from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from ..cadence import DelayDistributions
from ..config import Account, Platform
from ..content import PostJob
from ..device import DeviceController

Selector = dict[str, Any]          # uiautomator2 kwargs, e.g. {"resourceId": "..."} / {"text": "Next"} / {"description": "Create"}
Step = tuple[str, Callable[[Any], None]]


class SelectorNotFound(RuntimeError):
    pass


class VerifyFailed(RuntimeError):
    pass


@dataclass
class PostResult:
    ok: bool
    platform: Platform
    account_id: str
    job_id: str
    dry_run: bool
    steps: list[str] = field(default_factory=list)
    error: str = ""
    screenshot: Path | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None


class BaseDriver(ABC):
    platform: Platform
    package: str

    def __init__(
        self,
        account: Account,
        device: DeviceController,
        delays: DelayDistributions | None = None,
        *,
        dry_run: bool = True,
        rng: random.Random | None = None,
        sleep: Callable[[float], None] = time.sleep,
        browse_swipes: Sequence[int] = (3, 6),
    ):
        self.account, self.device = account, device
        self.delays = delays or DelayDistributions()
        self.dry_run = dry_run
        self.rng = rng or random.Random()
        self.sleep = sleep
        self.browse_swipes = tuple(browse_swipes)
        self.log = logging.getLogger(f"bot.driver.{self.platform.value}")

    @abstractmethod
    def post(self, job: PostJob) -> PostResult: ...

    # ------------------------------------------------------------------ step runner
    def _execute(self, job: PostJob, steps: list[Step]) -> PostResult:
        res = PostResult(False, self.platform, self.account.id, job.job_id, self.dry_run)
        d = None
        try:
            for name, fn in steps:
                tag = "[dry-run] " if self.dry_run else ""
                self.log.info("%s%s: %s", tag, self.account.id, name)
                res.steps.append(name)
                if self.dry_run:
                    continue
                if d is None:
                    d = self.device.session()
                fn(d)
                self.pause()
            res.ok = True
            if not self.dry_run:
                res.screenshot = self.device.screenshot(f"{job.job_id}_done")
        except Exception as e:  # noqa: BLE001 - any failure => failed result, caller handles retry/pause
            res.error = f"{type(e).__name__}: {e}"
            self.log.error("%s step %r failed: %s", self.account.id, res.steps[-1] if res.steps else "-", res.error)
            if not self.dry_run:
                try:
                    res.screenshot = self.device.screenshot(f"{job.job_id}_error")
                except Exception:  # noqa: BLE001
                    pass
        res.finished_at = datetime.now(timezone.utc)
        return res

    # ------------------------------------------------------------------ human-ish helpers (live only)
    def pause(self, scale: float = 1.0) -> None:
        self.sleep(self.delays.action_delay(self.rng) * scale)

    def find_any(self, d, candidates: Sequence[Selector], timeout: float = 6.0):
        """First selector that exists wins. Candidates are ordered most-specific -> most-generic."""
        for sel in candidates:
            el = d(**sel)
            if el.wait(timeout=timeout / max(1, len(candidates))):
                return el
        raise SelectorNotFound(f"none of {list(candidates)} found on screen (selectors need calibration)")

    def exists_any(self, d, candidates: Sequence[Selector], timeout: float = 2.0) -> bool:
        return any(d(**s).exists(timeout=timeout / max(1, len(candidates))) for s in candidates)

    def tap(self, d, candidates: Sequence[Selector], timeout: float = 6.0) -> None:
        """Click near (not exactly at) the element centre, like a finger would."""
        el = self.find_any(d, candidates, timeout)
        b = el.info.get("bounds") or {}
        if not b:
            el.click()
            return
        w, h = b["right"] - b["left"], b["bottom"] - b["top"]
        x = b["left"] + w * min(0.9, max(0.1, self.rng.gauss(0.5, 0.12)))
        y = b["top"] + h * min(0.9, max(0.1, self.rng.gauss(0.5, 0.12)))
        d.click(int(x), int(y))

    def type_into(self, d, candidates: Sequence[Selector], text: str) -> None:
        self.tap(d, candidates)
        self.pause(0.5)
        d.send_keys(text, clear=True)

    def swipe(self, d, direction: str = "up") -> None:
        w, h = d.window_size()
        x = w * self.rng.uniform(0.4, 0.6)
        a, b = (self.rng.uniform(0.70, 0.80), self.rng.uniform(0.25, 0.35))
        y1, y2 = (h * a, h * b) if direction == "up" else (h * b, h * a)
        d.swipe(x, y1, x + self.rng.uniform(-30, 30), y2, duration=self.rng.uniform(0.15, 0.45))

    def browse(self, d) -> None:
        """Session shape: scroll the feed with real dwell before acting (never open -> post -> close)."""
        n = self.rng.randint(*self.browse_swipes)
        for _ in range(n):
            self.swipe(d, "up")
            self.sleep(self.rng.uniform(2.0, 9.0))  # dwell on a video/post

    def go_home(self, d) -> None:
        d.press("home")
