"""CLI: python -m bot.cli {doctor,connect,status,queue,post,watch,schedule}"""
from __future__ import annotations

import argparse
import logging
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from .config import (Account, Config, ConfigError, Validation, load_accounts, load_config, validate_accounts)
from .content import ContentPipeline, PostJob
from .orchestrator import Orchestrator
from .state import StateStore

UTC = timezone.utc


def fmt_local(dt: datetime | None, tz) -> str:
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M %Z") if dt else "-"


class Ctx:
    """Everything loaded + validated once per command."""

    def __init__(self, args: argparse.Namespace, *, dry_run: bool | None = None):
        self.cfg: Config = load_config(args.config)
        self.dry_run = self.cfg.dry_run if dry_run is None else dry_run
        if getattr(args, "live", False):
            self.dry_run = False
        if getattr(args, "dry_run", False):
            self.dry_run = True
        self.accounts_list: list[Account] = load_accounts(args.accounts or self.cfg.path(self.cfg.instance.accounts_file))
        self.validation = validate_accounts(self.accounts_list, self.cfg)
        self.accounts = {a.id: a for a in self.accounts_list}
        self.pipeline = ContentPipeline(args.jobs or self.cfg.path(self.cfg.instance.jobs_file), self.accounts,
                                        allow_missing_assets=self.dry_run)
        self.jobs: list[PostJob]
        self.jobs, jv = self.pipeline.load()
        self.validation.extend(jv)

    def orchestrator(self) -> Orchestrator:
        self.validation.raise_if_errors()
        store = StateStore(self.cfg.runtime_dir, persist=not self.dry_run)
        return Orchestrator(self.cfg, self.accounts, self.jobs, store, dry_run=self.dry_run)


def print_validation(v: Validation) -> None:
    for w in v.warnings:
        print(f"  WARN  {w}")
    for e in v.errors:
        print(f"  ERROR {e}")


# ----------------------------------------------------------------------------- commands
def cmd_doctor(args) -> int:
    rows: list[tuple[str, str, str]] = []  # (level, name, detail)
    ok_hard = True
    rows.append(("OK" if sys.version_info >= (3, 10) else "FAIL", "python", sys.version.split()[0] + " (need >=3.10)"))
    try:
        ctx = Ctx(args)
    except ConfigError as e:
        for err in e.errors:
            rows.append(("FAIL", "config", err))
        ctx = None
    if ctx:
        rows.append(("OK", "config", f"{ctx.cfg.source.name}; mode={'DRY-RUN' if ctx.dry_run else 'LIVE'}"))
        v = ctx.validation
        rows.append(("OK" if v.ok else "FAIL", "accounts+jobs", f"{len(ctx.accounts_list)} accounts, {len(ctx.jobs)} valid jobs"
                     + ("" if v.ok else f", {len(v.errors)} error(s)")))
        for e in v.errors:
            rows.append(("FAIL", "validate", e))
        for w in v.warnings:
            rows.append(("WARN", "validate", w))
        i = ctx.cfg.instance
        live_lvl = "WARN" if ctx.dry_run else "FAIL"  # missing tools only block LIVE mode
        for name, b in (("adb", i.adb_bin), ("scrcpy", i.scrcpy_bin), ("ffmpeg", i.ffmpeg_bin)):
            p = shutil.which(b)
            lvl = "OK" if p else ("WARN" if name == "ffmpeg" else live_lvl)
            rows.append((lvl, name, p or f"not found ('{b}'); needed for live mode -> setup/instance_setup.sh"))
        try:
            import uiautomator2  # noqa: F401
            rows.append(("OK", "uiautomator2", "importable"))
        except ImportError:
            rows.append((live_lvl, "uiautomator2", "not installed (live mode only): pip install -r requirements.txt"))
        from .device import DeviceController
        for d in ctx.cfg.devices:
            dc = DeviceController(d.serial, d.adb_addr, adb_bin=i.adb_bin, dry_run=False)
            up = dc.is_connected()
            rows.append(("OK" if up else live_lvl, f"device {d.label or d.serial}",
                         "connected" if up else f"not connected ({d.adb_addr}); run `connect` once the tunnel is up"))
            if up:
                for a in ctx.accounts_list:
                    if a.device_serial == d.serial and dc.read_timezone() not in ("", a.geo.tz):
                        rows.append(("WARN", f"tz {a.id}", f"phone tz {dc.read_timezone()!r} != account tz {a.geo.tz}"))
    for lvl, name, detail in rows:
        print(f"[{lvl:4}] {name:<18} {detail}")
        ok_hard &= lvl != "FAIL"
    if getattr(args, "strict", False):
        ok_hard &= not any(r[0] == "WARN" for r in rows)
    print("doctor:", "all required checks passed" if ok_hard else "FAILED")
    return 0 if ok_hard else 1


def cmd_connect(args) -> int:
    ctx = Ctx(args)
    orch = ctx.orchestrator()
    code = 0
    for d in ctx.cfg.devices:
        dev = orch.device_for(next(a for a in ctx.accounts_list if a.device_serial == d.serial)) \
            if any(a.device_serial == d.serial for a in ctx.accounts_list) else None
        if dev is None:
            continue
        ok = dev.connect() and (ctx.dry_run or dev.is_connected())
        print(f"{d.label or d.serial}: {'connected' if ok else 'FAILED'}{' (dry-run)' if ctx.dry_run else ''}")
        code |= 0 if ok else 1
    return code


def cmd_verify_ip(args) -> int:
    from .netverify import (
        classify_with_report,
        fetch_egress_ip,
        fetch_ip_report,
    )
    from .device import DeviceController
    ctx = Ctx(args)
    accts = ctx.accounts_list
    if getattr(args, "account", None):
        accts = [a for a in accts if a.id == args.account]
    if not accts:
        print("no matching accounts (set --account to a configured id)", file=sys.stderr)
        return 1
    expected_ip   = getattr(args, "ip", None)
    expected_geo  = getattr(args, "geo", None)
    seen: set[str] = set()
    all_ok = True
    for acc in accts:
        if acc.device_serial in seen:
            continue
        seen.add(acc.device_serial)
        dev = DeviceController(acc.device_serial, dry_run=getattr(args, "dry_run", True))
        geo_str = expected_geo or (getattr(acc, "geo", None) or None)
        # geo may be a dataclass (Geo) or a plain string
        if geo_str is not None and not isinstance(geo_str, str):
            geo_str = str(getattr(geo_str, "country", "")) or str(geo_str)
        print(f"\n=== [{acc.id}]  device={acc.device_serial}  ip={acc.ip or '(unset)'}  geo={geo_str} ===")
        ip = fetch_egress_ip(dev, timeout=15.0)
        if ip is None:
            print("  IP: UNKNOWN (phone has no data connection)")
            all_ok = False
            continue
        print(f"  IP: {ip}")
        report = fetch_ip_report("ipinfo.io/json")
        if report.ip is None:
            report.ip = ip
        result = classify_with_report(report, expected_ip=expected_ip or (acc.ip or None),
                                      expected_geo=geo_str)
        for k, v in result.items():
            if k in ("ok", "checks", "verdict"):
                continue
            if isinstance(v, list):
                print(f"  {k}: {', '.join(v)}")
            elif isinstance(v, bool):
                print(f"  {k}: {'PASS' if v else 'FAIL'}")
            else:
                print(f"  {k}: {v}")
        status = "PASS" if result["ok"] else "FAIL"
        print(f"  >>> {status}: {result['verdict']}")
        if not result["ok"]:
            all_ok = False
    print()
    return 0 if all_ok else 1


def cmd_status(args) -> int:
    ctx = Ctx(args)
    orch = ctx.orchestrator() if ctx.validation.ok else None
    print(f"mode={'DRY-RUN' if ctx.dry_run else 'LIVE'}  config={ctx.cfg.source.name}  instance={ctx.cfg.instance.name}")
    print_validation(ctx.validation)
    if not orch:
        return 1
    now = datetime.now(UTC)
    hdr = f"{'account':<10}{'plat':<7}{'user':<18}{'device':<22}{'ip':<14}{'geo':<26}{'day':>3} {'cap':>3} {'today':>5}  {'health':<12}{'last post (local)':<22}paused"
    print(hdr)
    for a in ctx.accounts_list:
        st = orch.state_for(a, now)
        d = now.astimezone(a.tz).date()
        from .cadence import count_on_date
        n = count_on_date(st.post_times(), a.tz, d)
        pu = st.paused_until if st.is_paused(now) else (a.health.value if a.health.value == "paused" else "-")
        last = fmt_local(datetime.fromisoformat(st.last_post_at), a.tz) if st.last_post_at else "-"
        print(f"{a.id:<10}{a.platform.value:<7}{a.username:<18}{a.device_serial:<22}{a.ip:<14}"
              f"{a.geo.country + ' ' + a.geo.tz:<26}{st.warmup_day(d):>3} {orch.cap_today(a, st, d):>3} {n:>5}  "
              f"{st.health:<12}{last:<22}{pu}")
    print(f"pending jobs: {len(orch.pending_jobs())}")
    return 0


def cmd_queue(args) -> int:
    ctx = Ctx(args)
    orch = ctx.orchestrator()
    plan = orch.plan()
    print(f"{len(plan)} pending job(s):")
    for p in plan:
        a = ctx.accounts[p.job.account_id]
        when = fmt_local(p.scheduled_at, a.tz) if p.scheduled_at else "BLOCKED"
        print(f"  {p.job.job_id:<8} {a.id:<9} {when:<24} {p.note}")
    return 0


def cmd_post(args) -> int:
    ctx = Ctx(args)
    orch = ctx.orchestrator()
    if args.account not in ctx.accounts:
        print(f"unknown account {args.account!r}; known: {sorted(ctx.accounts)}", file=sys.stderr)
        return 2
    acc = ctx.accounts[args.account]
    jobs = [j for j in orch.pending_jobs(acc.id) if not args.job or j.job_id == args.job]
    if not jobs:
        print(f"no pending job for {acc.id}" + (f" with id {args.job}" if args.job else ""))
        return 2
    job = jobs[0]
    gate = orch.gate(acc)
    mode = "DRY-RUN" if ctx.dry_run else "LIVE"
    print(f"[{mode}] posting job {job.job_id} -> {acc.id} ({acc.platform.value}, @{acc.username}) on {acc.device_serial}")
    for g in gate:
        print(f"  gate: {g}" + (" (ignored in dry-run)" if ctx.dry_run else ""))
    try:
        res = orch.run_job(job, force=args.force)
    except PermissionError as e:
        print(f"refused: {e} (use --force to override)", file=sys.stderr)
        return 3
    for i, s in enumerate(res.steps, 1):
        print(f"  {i:>2}. {s}")
    print(f"result: {'OK' if res.ok else 'FAILED ' + res.error}" + (" (simulated)" if res.dry_run else ""))
    return 0 if res.ok else 1


def cmd_watch(args) -> int:
    ctx = Ctx(args)
    orch = ctx.orchestrator()
    accs = [ctx.accounts[args.account]] if args.account else ctx.accounts_list
    code = 0
    for a in accs:
        r = orch.watch(a)
        print(f"{a.id:<10}{r.health.value:<13}{'(simulated) ' if r.simulated else ''}{r.detail}")
    return code


def cmd_schedule(args) -> int:
    ctx = Ctx(args)
    orch = ctx.orchestrator()
    print(f"scheduler started ({'DRY-RUN' if ctx.dry_run else 'LIVE'}), poll={args.poll}s, once={args.once}")
    try:
        orch.schedule(args.poll, max_iterations=1 if args.once else None)
    except KeyboardInterrupt:
        print("stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="python -m bot.cli", description=__doc__)
    ap.add_argument("--config", help="config yaml (default config.yaml, else config.example.yaml)")
    ap.add_argument("--accounts", help="accounts yaml override")
    ap.add_argument("--jobs", help="jobs yaml override")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, dry=True):
        p = sub.add_parser(name, help=help_)
        p.set_defaults(fn=fn)
        if dry:
            g = p.add_mutually_exclusive_group()
            g.add_argument("--dry-run", action="store_true", help="force dry-run (no phone)")
            g.add_argument("--live", action="store_true", help="force live mode")
        return p

    add("doctor", cmd_doctor, "check python/adb/scrcpy/device/config/state", dry=False).add_argument(
        "--strict", action="store_true", help="treat warnings as failures")
    add("connect", cmd_connect, "adb connect to every configured device")
    p = add("verify-ip", cmd_verify_ip, "prove the phone egresses via SIM (IP/ASN/geo)", dry=False)
    p.add_argument("--account", help="account id (reads its ip/geo from accounts.yaml)")
    p.add_argument("--ip", help="expected egress IP (from accounts.yaml)")
    p.add_argument("--geo", help="expected geo, e.g. US or United States")
    add("status", cmd_status, "per-account identity, warm-up day, caps, health")
    add("queue", cmd_queue, "show the planned schedule for pending jobs")
    p = add("post", cmd_post, "post the next pending job for an account now")
    p.add_argument("--account", required=True)
    p.add_argument("--job", help="specific job id")
    p.add_argument("--force", action="store_true", help="LIVE only: ignore cap/window/pause gates")
    p = add("watch", cmd_watch, "run the shadowban/account-status watcher")
    p.add_argument("--account")
    p = add("schedule", cmd_schedule, "loop: post due jobs + periodic watcher")
    p.add_argument("--poll", type=float, default=60.0)
    p.add_argument("--once", action="store_true", help="single iteration then exit")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.fn(args)
    except ConfigError as e:
        for err in e.errors:
            print(f"ERROR {err}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
