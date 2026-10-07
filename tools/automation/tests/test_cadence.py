import random
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.cadence import (CadencePolicy, DailyCaps, DelayDistributions, RampSchedule, count_on_date,  # noqa: E402
                         in_window, near_top_of_hour, next_post_time)

UTC = timezone.utc
NY = ZoneInfo("America/New_York")
TOKYO = ZoneInfo("Asia/Tokyo")
POLICY = CadencePolicy()


def test_action_delay_range_and_spread():
    rng = random.Random(1)
    xs = [DelayDistributions().action_delay(rng) for _ in range(2000)]
    assert all(1.5 <= x <= 6.0 for x in xs)
    assert len({round(x, 2) for x in xs}) > 200          # randomized, not a constant
    assert 2.3 < sorted(xs)[1000] < 3.8                   # median near 3s


def test_post_gap_is_minutes_scale():
    rng = random.Random(2)
    gaps = [DelayDistributions().post_gap(rng) for _ in range(1000)]
    assert all(timedelta(minutes=20) <= g <= timedelta(minutes=240) for g in gaps)


@pytest.mark.parametrize("platform", ["ig", "tiktok"])
def test_ramp_monotonic_and_bounded(platform):
    caps = [POLICY.daily_cap(platform, d) for d in range(1, 60)]
    assert caps == sorted(caps)
    assert max(caps[:7]) <= 2                              # week 1 is light
    assert caps[-1] == POLICY.ramps[platform].steady
    assert max(caps) <= DailyCaps().for_platform(platform)


def test_steady_state_ranges():
    assert 3 <= POLICY.daily_cap("ig", 30) <= 5
    assert 4 <= POLICY.daily_cap("tiktok", 30) <= 8


def test_hard_ceiling_beats_ramp():
    r = RampSchedule((1, 1, 1, 1, 2, 2, 2), steady=10)
    assert r.cap(30, ceiling=5) == 5


def _simulate(tz, cap, days=6, seed=0, start=datetime(2026, 3, 2, 3, 0, tzinfo=UTC)):
    rng, hist, now = random.Random(seed), [], start
    for _ in range(cap * days):
        t = next_post_time(now, tz, hist, lambda d: cap, POLICY, rng)
        hist.append(t)
        now = t
    return hist


@pytest.mark.parametrize("tz", [NY, TOKYO])
@pytest.mark.parametrize("seed", range(5))
def test_caps_window_gaps_and_no_top_of_hour(tz, seed):
    hist = _simulate(tz, cap=4, seed=seed)
    for h in hist:
        assert in_window(h, tz, POLICY), h.astimezone(tz)
        assert not near_top_of_hour(h.astimezone(tz), POLICY.top_of_hour_guard_min)
    for d in {h.astimezone(tz).date() for h in hist}:
        assert count_on_date(hist, tz, d) <= 4
    assert all(b - a >= timedelta(minutes=20) for a, b in zip(hist, hist[1:]))


def test_cap_forces_next_local_day():
    rng = random.Random(3)
    now = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)         # 09:00 NY
    first = next_post_time(now, NY, [], lambda d: 1, POLICY, rng)
    second = next_post_time(first, NY, [first], lambda d: 1, POLICY, rng)
    assert second.astimezone(NY).date() > first.astimezone(NY).date()


def test_timezone_correctness_same_instant_different_local_day():
    # 2026-03-02 02:00 UTC is night in NY (21:00 on 03-01 -> inside window) but 11:00 in Tokyo.
    now = datetime(2026, 3, 2, 2, 0, tzinfo=UTC)
    ny = next_post_time(now, NY, [], lambda d: 3, POLICY, random.Random(4))
    tk = next_post_time(now, TOKYO, [], lambda d: 3, POLICY, random.Random(4))
    assert 8 <= ny.astimezone(NY).hour < 22 and 8 <= tk.astimezone(TOKYO).hour < 22
    assert tk - now < timedelta(hours=1)                  # Tokyo is mid-morning: ~immediate
    assert ny - now < timedelta(hours=1)                  # NY 21:00 local: still in window (before 22:00) or next morning


def test_before_window_waits_for_local_morning():
    now = datetime(2026, 3, 2, 8, 0, tzinfo=UTC)          # 03:00 NY
    t = next_post_time(now, NY, [], lambda d: 2, POLICY, random.Random(5)).astimezone(NY)
    assert t.date() == date(2026, 3, 2) and t.hour == 8 and t.minute <= 50


def test_earliest_not_before_is_respected():
    now = datetime(2026, 3, 2, 14, 0, tzinfo=UTC)
    earliest = datetime(2026, 3, 4, 12, 30, tzinfo=NY)
    t = next_post_time(now, NY, [], lambda d: 2, POLICY, random.Random(6), earliest=earliest)
    assert t >= earliest


def test_zero_cap_raises():
    with pytest.raises(RuntimeError):
        next_post_time(datetime(2026, 3, 2, 14, tzinfo=UTC), NY, [], lambda d: 0, POLICY, random.Random(0))


def test_dst_transition_window_still_local():
    # US spring-forward 2026-03-08: window must still be 08:00-22:00 local
    hist = _simulate(NY, cap=3, days=4, start=datetime(2026, 3, 7, 12, 0, tzinfo=UTC))
    assert all(in_window(h, NY, POLICY) for h in hist)
