"""Cadence engine: delay distributions, warm-up ramp, daily caps, next-post-time in account tz.

Pure functions / frozen dataclasses; randomness always comes from an injected `random.Random`.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Callable, Iterable, Mapping

UTC = timezone.utc


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


@dataclass(frozen=True)
class DelayDistributions:
    """Lognormal delays (right-skewed like human pauses), clamped to hard bounds."""
    action_median_s: float = 3.0
    action_sigma: float = 0.45
    action_min_s: float = 1.5
    action_max_s: float = 6.0
    gap_median_min: float = 60.0
    gap_sigma: float = 0.5
    gap_min_min: float = 20.0
    gap_max_min: float = 240.0

    def action_delay(self, rng: random.Random) -> float:
        """Seconds between two UI actions."""
        x = rng.lognormvariate(math.log(self.action_median_s), self.action_sigma)
        return _clamp(x, self.action_min_s, self.action_max_s)

    def post_gap(self, rng: random.Random) -> timedelta:
        """Minimum spacing between two posts of one account."""
        x = rng.lognormvariate(math.log(self.gap_median_min), self.gap_sigma)
        return timedelta(minutes=_clamp(x, self.gap_min_min, self.gap_max_min))


@dataclass(frozen=True)
class DailyCaps:
    """Absolute per-platform ceilings; nothing (ramp, config) may exceed these."""
    ig: int = 5
    tiktok: int = 8

    def for_platform(self, platform: str) -> int:
        return {"ig": self.ig, "tiktok": self.tiktok}[platform]


@dataclass(frozen=True)
class RampSchedule:
    """Warm-up day -> max posts/day. Days 1-7 use `week1`; 8..ramp_days interpolate linearly to `steady`."""
    week1: tuple[int, ...]
    steady: int
    ramp_days: int = 14

    def cap(self, day: int, ceiling: int | None = None) -> int:
        day = max(1, day)
        if day <= 7:
            c = self.week1[day - 1]
        elif day >= self.ramp_days:
            c = self.steady
        else:
            base = self.week1[-1]
            c = base + round((self.steady - base) * (day - 7) / (self.ramp_days - 7))
        return min(c, ceiling) if ceiling is not None else c


@dataclass(frozen=True)
class CadencePolicy:
    window_start: time = time(8, 0)
    window_end: time = time(22, 0)
    top_of_hour_guard_min: int = 3
    delays: DelayDistributions = field(default_factory=DelayDistributions)
    caps: DailyCaps = field(default_factory=DailyCaps)
    ramps: Mapping[str, RampSchedule] = field(default_factory=lambda: {
        "ig": RampSchedule((1, 1, 1, 1, 2, 2, 2), 4),
        "tiktok": RampSchedule((1, 1, 1, 2, 2, 2, 2), 6),
    })

    def daily_cap(self, platform: str, warmup_day: int) -> int:
        return self.ramps[platform].cap(warmup_day, self.caps.for_platform(platform))

    @classmethod
    def from_cfg(cls, c) -> "CadencePolicy":
        """Build from bot.config.CadenceCfg (duck-typed to keep this module dependency-free)."""
        from .config import parse_hhmm
        d = c.delays
        return cls(
            window_start=parse_hhmm(c.window_start), window_end=parse_hhmm(c.window_end),
            top_of_hour_guard_min=c.top_of_hour_guard_min,
            delays=DelayDistributions(**vars(d)),
            caps=DailyCaps(ig=c.platforms["ig"].hard_max, tiktok=c.platforms["tiktok"].hard_max),
            ramps={k: RampSchedule(tuple(v.week1), v.steady, c.ramp_days) for k, v in c.platforms.items()},
        )


# ----------------------------------------------------------------------------- time helpers
def window_bounds(d: date, tz: tzinfo, policy: CadencePolicy) -> tuple[datetime, datetime]:
    return (datetime.combine(d, policy.window_start, tzinfo=tz), datetime.combine(d, policy.window_end, tzinfo=tz))


def in_window(dt: datetime, tz: tzinfo, policy: CadencePolicy) -> bool:
    ws, we = window_bounds(dt.astimezone(tz).date(), tz, policy)
    return ws <= dt.astimezone(tz) < we


def near_top_of_hour(dt: datetime, guard_min: int) -> bool:
    m = dt.minute
    return m < guard_min or m >= 60 - guard_min


def count_on_date(history: Iterable[datetime], tz: tzinfo, d: date) -> int:
    return sum(1 for h in history if h.astimezone(tz).date() == d)


def _avoid_top_of_hour(local: datetime, guard: int, rng: random.Random) -> datetime:
    if not near_top_of_hour(local, guard):
        return local
    hour = local.replace(minute=0, second=0, microsecond=0)
    if local.minute >= 60 - guard:
        hour += timedelta(hours=1)
    return hour + timedelta(minutes=rng.uniform(guard + 1, guard + 15))


def next_post_time(
    now: datetime,
    tz: tzinfo,
    history: Iterable[datetime],
    cap_for_date: Callable[[date], int],
    policy: CadencePolicy,
    rng: random.Random,
    earliest: datetime | None = None,
) -> datetime:
    """Next allowed post instant (UTC-aware) for one account.

    Honors: min inter-post gap after the last post, per-local-day cap, the account-local posting window,
    optional `earliest` (job's "not before"), and avoids :00 +/- guard minutes. Randomized throughout.
    """
    hist = sorted(h.astimezone(UTC) for h in history)
    t = max(now.astimezone(UTC), earliest.astimezone(UTC) if earliest else now.astimezone(UTC))
    if hist:
        t = max(t, hist[-1] + policy.delays.post_gap(rng))
    guard = policy.top_of_hour_guard_min
    for _ in range(90):  # days to scan
        local = t.astimezone(tz)
        d = local.date()
        ws, we = window_bounds(d, tz, policy)
        next_day_start = lambda: (  # noqa: E731
            window_bounds(d + timedelta(days=1), tz, policy)[0] + timedelta(minutes=rng.uniform(0, 45))
        ).astimezone(UTC)
        if count_on_date(hist, tz, d) >= cap_for_date(d):
            t = next_day_start()
            continue
        if local < ws:
            t = (ws + timedelta(minutes=rng.uniform(0, 45))).astimezone(UTC)
            continue
        if local >= we:
            t = next_day_start()
            continue
        adj = _avoid_top_of_hour(local, guard, rng) + timedelta(seconds=rng.uniform(0, 59))
        adj = _avoid_top_of_hour(adj, guard, rng)  # seconds jitter may roll into the guard zone
        if adj >= we:
            t = next_day_start()
            continue
        return adj.astimezone(UTC)
    raise RuntimeError("no postable slot within 90 days (are all daily caps 0?)")
