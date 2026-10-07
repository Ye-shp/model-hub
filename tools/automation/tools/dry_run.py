#!/usr/bin/env python3
"""End-to-end dry run: load config+accounts+jobs, validate, plan the cycle (cadence/caps/warm-up), 'post' each
planned job through the right driver in dry-run, run the watcher in dry-run. No phone/network. Exits 0 on success."""
from __future__ import annotations

import sys
from argparse import Namespace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.cadence import count_on_date  # noqa: E402
from bot.cli import Ctx, fmt_local, print_validation  # noqa: E402
from bot.config import ConfigError  # noqa: E402


def main() -> int:
    try:
        ctx = Ctx(Namespace(config=None, accounts=None, jobs=None, dry_run=True, live=False), dry_run=True)
        print(f"== config: {ctx.cfg.source.name} | accounts: {len(ctx.accounts_list)} | jobs: {len(ctx.jobs)} | mode: DRY-RUN")
        print_validation(ctx.validation)
        orch = ctx.orchestrator()
    except ConfigError as e:
        print("validation failed:\n  " + "\n  ".join(e.errors))
        return 1

    now = datetime.now(timezone.utc)
    print("\n== identity map (1 account <-> 1 device <-> 1 IP <-> 1 geo)")
    for a in ctx.accounts_list:
        st = orch.state_for(a, now)
        d = now.astimezone(a.tz).date()
        print(f"  {a.id:<9}{a.platform.value:<7}@{a.username:<16} dev={a.device_serial} ip={a.ip} {a.geo.country}/{a.geo.tz}/{a.geo.lang}"
              f"  warm-up day {st.warmup_day(d)}, cap today {orch.cap_today(a, st, d)}, posted today {count_on_date(st.post_times(), a.tz, d)}")

    print("\n== cadence ramp (max posts/day by warm-up day)")
    for p in ("ig", "tiktok"):
        print(f"  {p:<7}" + " ".join(f"d{n}:{orch.policy.daily_cap(p, n)}" for n in (1, 2, 3, 4, 5, 6, 7, 9, 11, 14, 30)))

    plan = orch.plan(now)
    print(f"\n== plan ({len(plan)} pending jobs; times in each account's local tz, window "
          f"{orch.policy.window_start:%H:%M}-{orch.policy.window_end:%H:%M}, no :00 posts)")
    for p in plan:
        a = ctx.accounts[p.job.account_id]
        print(f"  {p.job.job_id:<8}{a.id:<9}{fmt_local(p.scheduled_at, a.tz) if p.scheduled_at else 'BLOCKED':<24}{p.note}")

    print("\n== executing each planned job through its driver (dry-run)")
    failures = 0
    for p in plan:
        if p.scheduled_at is None:
            continue
        res = orch.run_job(p.job)
        print(f"  [{p.job.job_id}] {res.platform.value} via {type(orch.driver_for(ctx.accounts[p.job.account_id])).__name__}: "
              f"{len(res.steps)} steps -> {'OK (simulated)' if res.ok else 'FAILED ' + res.error}")
        failures += not res.ok

    print("\n== watcher (dry-run)")
    for a in ctx.accounts_list:
        r = orch.watch(a)
        print(f"  {a.id:<9}{r.health.value:<8}{r.detail}")
    print("\ndry run complete; nothing was written to state/runtime and no device was contacted.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
