"""Tests for the pre-flight egress-IP guard (Orchestrator._check_egress)."""
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Account, Geo, InstanceCfg, Platform  # noqa: E402
from bot.device import DeviceError  # noqa: E402
from bot.orchestrator import Orchestrator  # noqa: E402


def _orch(verify_ip: str, dry_run: bool = False):
    o = Orchestrator.__new__(Orchestrator)
    o.cfg = SimpleNamespace(instance=InstanceCfg(verify_ip=verify_ip))
    o.dry_run = dry_run
    o.log = __import__("logging").getLogger("test.egress")
    return o


def _account(ip: str) -> Account:
    return Account(id="ig_main", platform=Platform.IG, username="u", device_serial="dev",
                   ip=ip, geo=Geo(tz="America/New_York", lang="en-US", country="US"))


def test_off_skips():
    o = _orch("off")
    o.device_for = lambda a: None
    o._check_egress(_account("1.2.3.4"))          # no call to device -> no error
    assert o.device_for is not None


def test_warn_passes_when_matching():
    o = _orch("warn")
    dev = SimpleNamespace(egress_ip=lambda: "73.78.11.22")
    o.device_for = lambda a: dev
    o._check_egress(_account("73.78.11.22"))      # matches -> no raise


def test_warn_logs_when_mismatched():
    o = _orch("warn")
    o.device_for = lambda a: SimpleNamespace(egress_ip=lambda: "93.125.82.10")
    o._check_egress(_account("73.78.11.22"))   # mismatch -> warn (logged), but does NOT raise


def test_hard_blocks_when_mismatched():
    o = _orch("hard")
    o.device_for = lambda a: SimpleNamespace(egress_ip=lambda: "93.125.82.10")
    try:
        o._check_egress(_account("73.78.11.22"))
        raise AssertionError("expected DeviceError")
    except DeviceError:
        pass


def test_dry_run_skips():
    o = _orch("hard", dry_run=True)
    o.device_for = lambda a: (_ for _ in ()).throw(RuntimeError("should not be called"))
    o._check_egress(_account("73.78.11.22"))
