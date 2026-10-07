"""Orchestrator: job queue + cadence/caps/warm-up gating + driver dispatch + state + watcher + retry/backoff."""
from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from .cadence import CadencePolicy, in_window, count_on_date, next_post_time
from .config import Account, Config, Health, Platform
from .content import PostJob
from .device import DeviceController, DeviceError
from .drivers.base import BaseDriver, PostResult
from .drivers.instagram import InstagramDriver
from .drivers.tiktok import TikTokDriver
from .state import AccountState, StateStore, iso, parse_iso
from .watcher import ShadowbanWatcher, WatchResult
from .mediaprep import MediaPrep, MediaPrepError
from .aifinger import scan as _afscan, AIFingerReport as _ScanReport

log = logging.getLogger("bot.orchestrator")
UTC = timezone.utc
_STALE = timedelta(minutes=30)


@dataclass
class PlannedPost:
    job: PostJob
    scheduled_at: datetime | None     # UTC; None when blocked
    note: str = ""                    # why blocked, or cap/warm-up info


class Orchestrator:
    def __init__(
        self,
        cfg: Config,
        accounts: dict[str, Account],
        jobs: list[PostJob],
        store: StateStore,
        *,
        dry_run: bool,
        rng: random.Random | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.cfg, self.accounts, self.jobs, self.store = cfg, accounts, jobs, store
        self.dry_run = dry_run
        self.rng = rng or random.Random()
        self.clock, self.sleep = clock, sleep
        self.policy = CadencePolicy.from_cfg(cfg.cadence)
        self.watcher = ShadowbanWatcher(cfg.watcher, dry_run=dry_run, instagram_package=cfg.instance.instagram_package,
                                        tiktok_package=cfg.instance.tiktok_package)
        self._devices: dict[str, DeviceController] = {}
        self._sched: dict[str, datetime] = {}   # job_id -> chosen slot (stable between polls)
        # Anti-AI-detection media pipeline (see bot/mediaprep.py + bot/aifinger.py).
        # Built lazily here so the orchestrator can run in dry-run without ffmpeg.
        self._mediaprep: MediaPrep | None = MediaPrep(cfg.mediaprep, dry_run=dry_run)

    # ------------------------------------------------------------------ wiring
    def device_for(self, account: Account) -> DeviceController:
        if account.device_serial not in self._devices:
            dc = self.cfg.device(account.device_serial)
            i = self.cfg.instance
            self._devices[account.device_serial] = DeviceController(
                account.device_serial, dc.adb_addr if dc else None, dry_run=self.dry_run, adb_bin=i.adb_bin,
                scrcpy_bin=i.scrcpy_bin, scrcpy_args=i.scrcpy_args, screenshots_dir=self.cfg.path(i.screenshots_dir))
        return self._devices[account.device_serial]

    def prepare_media(self, job: PostJob) -> Path:
        """Scan + prep the video before it is pushed to the phone.

        Returns the path the driver should use (prepared file if the pipeline ran,
        otherwise the original).  In dry-run the pipeline is logged but not executed.
        """
        mp = self._mediaprep
        if mp is None or not mp.enabled:
            return job.asset_path

        src = job.asset_path
        if not src.exists():
            log.debug("asset not on disk yet (%s) — skipping media prep (dry-run)", src.name)
            return src

        # 1) scan the source
        pre_scan = _afscan(src)
        log.info("AI-finger pre-scan  %s -> %s (%d strong, %d weak)",
                 src.name, pre_scan.verdict, pre_scan.strong_count, pre_scan.weak_count)
        for f in pre_scan.findings:
            log.info("  [%s] %s", f.severity, f.detail)

        # 2) prepare (re-encode + strip + humanise)
        prep_res = mp.prepare(src, platform=job.platform.value)
        if not prep_res.ok:
            if self.dry_run:
                log.warning("media prep failed (dry-run, continuing): %s", prep_res.error)
                return src
            raise MediaPrepError(f"media prep failed for {src}: {prep_res.error}")

        # 3) scan the output
        post_scan = _afscan(Path(prep_res.dst))
        log.info("AI-finger post-scan %s -> %s (%d strong, %d weak)",
                 Path(prep_res.dst).name, post_scan.verdict,
                 post_scan.strong_count, post_scan.weak_count)
        for f in post_scan.findings:
            log.info("  [%s] %s", f.severity, f.detail)

        # 4) warn (not block) if the post-scan is worse than pre
        if post_scan.strong_count > pre_scan.strong_count:
            log.warning("post-scan has MORE strong findings than pre-scan "
                        "(%d -> %d); check mediaprep settings",
                        pre_scan.strong_count, post_scan.strong_count)

        log.info("media prep done: %s -> %s (%.1f MB, %s)",
                 src.name, Path(prep_res.dst).name, prep_res.size_bytes / 1e6,
                 "; ".join(prep_res.filters_applied) or "no-op")
        return Path(prep_res.dst)

    def driver_for(self, account: Account) -> BaseDriver:
        kw = dict(dry_run=self.dry_run, rng=self.rng, browse_swipes=self.cfg.cadence.browse_swipes, sleep=self.sleep)
        dev, delays = self.device_for(account), self.policy.delays
        if account.platform is Platform.IG:
            return InstagramDriver(account, dev, delays, **kw)
        i = self.cfg.instance
        return TikTokDriver(account, dev, delays, package=i.tiktok_package, reencode=i.reencode_tiktok,
                            ffmpeg_bin=i.ffmpeg_bin, **kw)

    def state_for(self, account: Account, now: datetime | None = None) -> AccountState:
        now = now or self.clock()
        return self.store.get(account.id, now.astimezone(account.tz).date(), account.warmup_day, account.health.value)

    def pending_jobs(self, account_id: str | None = None) -> list[PostJob]:
        return [j for j in self.jobs if not self.store.job_done(j.job_id) and (account_id in (None, j.account_id))]

    # ------------------------------------------------------------------ gating
    def blocked_reason(self, account: Account, st: AccountState, now: datetime) -> str:
        if account.health is Health.PAUSED:
            return "account health=paused in accounts file (manual hold)"
        if st.is_paused(now):
            return f"paused until {st.paused_until} ({st.pause_reason})"
        return ""

    def cap_today(self, account: Account, st: AccountState, d) -> int:
        return self.policy.daily_cap(account.platform.value, st.warmup_day(d))

    def gate(self, account: Account, now: datetime | None = None) -> list[str]:
        """Reasons a post right now would violate policy (empty list = fine)."""
        now = now or self.clock()
        st = self.state_for(account, now)
        out = []
        if r := self.blocked_reason(account, st, now):
            out.append(r)
        d = now.astimezone(account.tz).date()
        n, cap = count_on_date(st.post_times(), account.tz, d), self.cap_today(account, st, d)
        if n >= cap:
            out.append(f"daily cap reached ({n}/{cap}, warm-up day {st.warmup_day(d)})")
        if not in_window(now, account.tz, self.policy):
            out.append(f"outside local posting window {self.policy.window_start:%H:%M}-{self.policy.window_end:%H:%M} ({account.geo.tz})")
        return out

    # ------------------------------------------------------------------ planning
    def plan(self, now: datetime | None = None) -> list[PlannedPost]:
        now = now or self.clock()
        out: list[PlannedPost] = []
        hist: dict[str, list[datetime]] = {}
        for job in self.pending_jobs():
            acc = self.accounts[job.account_id]
            st = self.state_for(acc, now)
            if reason := self.blocked_reason(acc, st, now):
                out.append(PlannedPost(job, None, reason))
                continue
            h = hist.setdefault(acc.id, list(st.post_times()))
            t = self._sched.get(job.job_id)
            if t is None or t < now - _STALE:
                t = next_post_time(now, acc.tz, h, lambda d, a=acc, s=st: self.cap_today(a, s, d),
                                   self.policy, self.rng, earliest=job.post_time)
                self._sched[job.job_id] = t
            h.append(t)
            d = t.astimezone(acc.tz).date()
            out.append(PlannedPost(job, t, f"warm-up day {st.warmup_day(d)}, cap {self.cap_today(acc, st, d)}/day"))
        return out

    # ------------------------------------------------------------------ execution
    def run_job(self, job: PostJob, *, force: bool = False) -> PostResult:
        """Post one job now (with retry/backoff). Gating is enforced unless dry_run/force (caller prints `gate`)."""
        acc = self.accounts[job.account_id]
        now = self.clock()
        if not (force or self.dry_run) and (why := self.gate(acc, now)):
            raise PermissionError("; ".join(why))
        st = self.state_for(acc, now)
        # Anti-AI-detection: prepare media (scan + prep) before push.
        # Falls back to the original file if prep fails or is disabled.
        try:
            prepared = self.prepare_media(job)
            if prepared != job.asset_path:
                log.info("using prepped media: %s", prepared)
                job.asset_path = prepared
        except Exception as e:
            log.warning("media prep failed (%s); posting original", e)
        driver = self.driver_for(acc)
        self._check_egress(acc)
        res = driver.post(job)
        attempt = 0
        while not res.ok and attempt < self.cfg.cadence.max_retries:
            wait = self.cfg.cadence.retry_backoff_s * 2 ** attempt * self.rng.uniform(0.8, 1.4)
            log.warning("%s failed (%s); retry %d in %.0fs", job.job_id, res.error, attempt + 1, wait)
            self.sleep(wait)
            attempt += 1
            res = driver.post(job)
        done = self.clock()
        st.record_post(done, job.job_id, res.ok, ("dry-run " if self.dry_run else "") + (res.error or "ok"), job.hashtags)
        if res.ok:
            self.store.mark_job(job.job_id, "done", done)
        elif st.consecutive_failures >= self.cfg.cadence.pause_after_failures:
            st.pause(done, 1, f"{st.consecutive_failures} consecutive failures")
        self.store.save(st)
        self._drop_sched(acc.id)  # gap after this post changes the other jobs' slots
        if res.ok and self.cfg.watcher.enabled and self.cfg.watcher.check_after_post:
            self.watch(acc)
        return res

    def _check_egress(self, account: Account) -> None:
        """Pre-flight: the account must post over its own SIM IP (what IG/TikTok see), not Wi-Fi/tailnet.
        Live posts only. Mode from instance.verify_ip: warn (default) | hard | off."""
        mode = (self.cfg.instance.verify_ip or "warn").lower()
        if mode == "off" or self.dry_run or not account.ip:
            return
        ip = self.device_for(account).egress_ip()
        if ip is None:
            if mode == "hard":
                raise DeviceError("egress IP unknown; cannot confirm account.ip (set instance.verify_ip: off to bypass)")
            log.warning("egress IP unknown; expected %s", account.ip)
            return
        if ip == account.ip:
            log.info("egress OK: %s matches account.ip", ip)
            return
        msg = f"egress IP {ip} != account.ip {account.ip} (SIM off? Wi-Fi on? wrong account on this SIM?)"
        if mode == "hard":
            raise DeviceError(msg)
        log.warning(msg)

    def _drop_sched(self, account_id: str) -> None:
        for j in self.jobs:
            if j.account_id == account_id:
                self._sched.pop(j.job_id, None)

    def run_due(self, now: datetime | None = None) -> list[PostResult]:
        now = now or self.clock()
        results = []
        for p in self.plan(now):
            if p.scheduled_at is not None and p.scheduled_at <= now:
                try:
                    results.append(self.run_job(p.job))
                except PermissionError as e:
                    log.info("skip %s: %s", p.job.job_id, e)
                    self._sched.pop(p.job.job_id, None)
        return results

    # ------------------------------------------------------------------ watcher
    def watch(self, account: Account) -> WatchResult:
        now = self.clock()
        st = self.state_for(account, now)
        probe = st.last_hashtags[0] if st.last_hashtags else None
        r = self.watcher.check(account, self.device_for(account), probe)
        st.last_watch_at, st.shadowban_status, st.health = iso(now), r.health.value, r.health.value
        st.log(now, "watch", f"{r.health.value}: {r.detail}")
        if r.health in (Health.WARNING, Health.SHADOWBANNED) and not r.simulated:
            st.pause(now, self.cfg.watcher.pause_days, f"watcher: {r.detail}")
            self._drop_sched(account.id)
            log.warning("PAUSED %s for %dd: %s", account.id, self.cfg.watcher.pause_days, r.detail)
        self.store.save(st)
        return r

    def watch_due(self) -> list[WatchResult]:
        now, out = self.clock(), []
        for acc in self.accounts.values():
            last = parse_iso(self.state_for(acc, now).last_watch_at)
            if self.cfg.watcher.enabled and (last is None or (now - last).total_seconds() >= self.cfg.watcher.interval_hours * 3600):
                out.append(self.watch(acc))
        return out

    # ------------------------------------------------------------------ loop
    def schedule(self, poll_s: float = 60.0, max_iterations: int | None = None) -> None:
        i = 0
        while True:
            self.run_due()
            self.watch_due()
            i += 1
            if max_iterations is not None and i >= max_iterations:
                return
            self.sleep(poll_s)
