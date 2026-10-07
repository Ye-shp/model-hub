"""verify_ip.py — standalone CLI for phone-egress IP verification.

Two modes:
  1. Endpoint probe (default): hit an IP-lookup service from *this* machine and
     classify the IP/ASN/geo. Useful as a baseline, or to sanity-check a service.
  2. `--adb-serial <serial>`: ask the phone (via `adb shell curl ipinfo.io`) for
     its *actual* egress IP and classify that.

Both paths share the pure core in `bot.netverify`, so the logic is testable
without a phone.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from bot.netverify import (  # noqa: E402
    ENDPOINTS,
    IpReport,
    classify,
    classify_with_report,
    fetch_ip_report,
)


def _adb_ip(serial: str) -> IpReport:
    """Best-effort: read the phone's egress IP via adb shell + ipinfo.json."""
    cmd = [
        "adb", "-s", serial, "shell",
        "curl -s --max-time 8 https://ipinfo.io/json",
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if out.returncode == 0 and out.stdout.strip().startswith("{"):
            d = json.loads(out.stdout)
            return IpReport(
                endpoint="adb:curl",
                ip=d.get("ip"),
                asn=str(d.get("asn")) if d.get("asn") else None,
                org=d.get("org"),
                country=d.get("country"),
                city=d.get("city"),
                country_name=d.get("country_name"),
                raw=d,
            )
    except Exception:
        pass
    return IpReport(endpoint="adb:curl", ip=None, asn=None, org=None, country=None, city=None)


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--endpoint", default=ENDPOINTS[0], help=f"one of {list(ENDPOINTS)}")
    p.add_argument("--adb-serial", help="phone serial to verify via adb shell (overrides endpoint)")
    p.add_argument("--expected-ip", help="assert egress IP equals this (e.g. from accounts.yaml)")
    p.add_argument("--country", help="assert egress country equals this (ISO-2, e.g. US)")
    p.add_argument("--no-color", action="store_true")
    a = p.parse_args(argv)

    if a.adb_serial:
        r = _adb_ip(a.adb_serial)
        res = classify_with_report(r, expected_ip=a.expected_ip, expected_geo=a.country)
    else:
        r = fetch_ip_report(a.endpoint)
        res = classify_with_report(r, expected_ip=a.expected_ip, expected_geo=a.country)

    print(f"endpoint : {r.endpoint}")
    print(f"  ip      : {r.ip or '-'}")
    print(f"  asn     : {r.asn or '-'}")
    print(f"  org     : {r.org or '-'}")
    print(f"  country : {r.country or '-'}")
    print(f"  city    : {r.city or '-'}")
    if "cloud" in res:
        print(f"  cloud   : {res['cloud']}")
        print(f"  carrier : {res.get('carrier') or '-'}")
    print(f"verdict  : {res['verdict']}")
    if res.get("warnings"):
        print("warnings:")
        for w in res["warnings"]:
            print(f"  - {w}")
    return 0 if res["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
