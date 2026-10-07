"""Net-verification core: fetch the phone's public egress IP + classify it.

Used by the CLI (`bot.cli verify-ip`) and by `tools/verify_ip.py`. The logic is
pure and injectable (a `fetch` callable) so it is unit-testable without a phone.

API surface (kept stable):
    fetch_egress_ip(dev, timeout)  -> str | None      (from a DeviceController)
    fetch_ip_report(endpoint, ...) -> IpReport        (from a URL endpoint)
    classify(ip, expected_ip, expected_geo) -> dict   (ok / verdict / checks / ...)
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.request import Request, urlopen

log = logging.getLogger("netverify")
DEFAULT_TIMEOUT = 10

# Alternative IP-lookup endpoints (first one is the default).
ENDPOINTS = ("ipinfo.io/json", "ipapi.co/json", "ifconfig.co/json")

# ASN ranges that are almost never a real consumer carrier (datacenter / cloud / hosting).
# A hit is a *warning*, never a hard fail.
_CLOUD_ASNS = {
    14618,   # AWS
    15169,   # Google
    396982,  # DigitalOcean
    16509,   # Amazon
    26496,   # Hetzner
    13335,   # Cloudflare
    13414,   # Apple iCloud / CDN
    36459,   # OVH
    20473,   # Leaseweb
    8075,    # Microsoft
    32934,   # Fastly
    15234,   # Spotify / CDNs
    32445,   # Akamai
}

# Conservative consumer-carrier ASN sets (a hit is a *positive signal*; a miss is only a
# warning — carriers vary, and MVNOs can appear under a parent's ASN).
_CARRIER_HINTS: dict[str, set[int]] = {
    "US": {
        "t-mobile": {31635, 26658, 21436},
        "att": {7922, 20115},
        "verizon": {7922, 7160, 20115},
        "visible": {7160},
        "us_mobile": {7160, 7922, 20115},
    },
    "GB": {
        "ee": {8452, 5388, 20713},
        "vodafone": {23438},
        "three": {23438},
        "o2": {23438},
        "giffgaff": {5388, 8452},
    },
    "DE": {"telekom": {3320, 3323}, "vodafone": {24988}, "o2": {20211, 3320}},
    "FR": {"orange": {3267, 5588}, "sfr": {24988, 5588}, "bouygues": {8705}},
    "NL": {"kpn": {8694, 3352}, "vodafone": {24988}},
    "AU": {"telstra": {1239, 131547}, "optus": {38409}, "vodafone": {38409, 4782}},
}


def _default_fetch(url: str, timeout: int = DEFAULT_TIMEOUT) -> str:
    req = Request(url, headers={"User-Agent": "curl/8 (phone-ip-probe)"})
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


@dataclass
class IpReport:
    endpoint: str
    ip: str | None
    asn: str | None
    org: str | None
    country: str | None
    city: str | None
    country_name: str | None = None
    raw: dict | None = None


def fetch_ip_report(endpoint: str, fetch=_default_fetch, timeout: int = DEFAULT_TIMEOUT) -> IpReport:
    """Fetch + parse one IP-lookup endpoint. Never raises; missing fields are None."""
    try:
        body = fetch(f"https://{endpoint}", timeout)
        data = json.loads(body)
        return IpReport(
            endpoint=endpoint,
            ip=data.get("ip"),
            asn=str(data.get("asn")) if data.get("asn") is not None else None,
            org=data.get("org"),
            country=data.get("country"),
            city=data.get("city"),
            country_name=data.get("country_name"),
            raw=data,
        )
    except Exception as e:  # noqa: BLE001
        log.warning("endpoint %s failed: %s", endpoint, e)
        return IpReport(endpoint=endpoint, ip=None, asn=None, org=None, country=None, city=None)


def fetch_egress_ip(dev, timeout: float = 15.0) -> str | None:
    """Ask the phone (DeviceController) for its current public egress IP.

    `dev.egress_ip()` already shells out to ipinfo/icanhazip via adb and returns
    the dotted-quad string, or None when undetectable.
    """
    try:
        ip = dev.egress_ip()
    except Exception as e:  # noqa: BLE001
        log.warning("device.egress_ip() raised: %s", e)
        return None
    if not ip:
        return None
    if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip.strip()):
        log.warning("unexpected IP shape: %r", ip)
        return None
    return ip.strip()


def _as_int_asn(value) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    m = re.search(r"\d+", s)
    return int(m.group()) if m else None


def _carrier_name(country: str | None, asn: int | None) -> str | None:
    if not country or asn is None:
        return None
    hints = _CARRIER_HINTS.get(country.upper())
    if not hints:
        return None
    for name, set_ in hints.items():
        if asn in set_:
            return name
    return None


def classify(ip: str, expected_ip: str | None = None, expected_geo: str | None = None) -> dict:
    """Classify a phone egress IP.

    Returns a dict with at least:
        ok        bool          (True iff the expected-IP check passes, or no expected IP)
        verdict   str           (human-readable outcome)
        checks    dict[str, bool]
        warnings  list[str]
        cloud     bool          (True if ASN looks datacenter/hosting)
        carrier   str | None    (best-guess carrier name, when ASN matches a known set)
        ip        str           (the input, echoed)

    `expected_geo` is free-form: an ISO-2 country (US/GB/DE/...), a city, or a full
    string. Matching is case-insensitive substring; the actual country/city are only
    checked if we have a fetched IpReport. This function does *not* fetch on its own —
    pass the IP string; if you need ASN/org/geo, call fetch_ip_report first.
    """
    ip = (ip or "").strip()
    result: dict = {
        "ip": ip,
        "ok": True,
        "verdict": "",
        "checks": {},
        "warnings": [],
        "cloud": False,
        "carrier": None,
    }

    # --- expected IP -----------------------------------------------------------
    if expected_ip:
        expected = str(expected_ip).strip()
        match = ip == expected
        result["checks"]["expected_ip_match"] = match
        if not match:
            result["ok"] = False
            result["warnings"].append(f"expected {expected}, got {ip}")

    # --- expected geo ----------------------------------------------------------
    if expected_geo:
        geo = str(expected_geo).strip().lower()
        # We don't have country here unless a report is attached; best-effort:
        # accept any non-empty geo as "declared" and note it for the caller.
        result["checks"]["geo_declared"] = True
        result["geo_expected"] = geo

    # --- verdict ---------------------------------------------------------------
    if not ip:
        result["ok"] = False
        result["verdict"] = "no IP detected on the phone"
    elif not result["ok"]:
        result["verdict"] = "egress IP does NOT match the account's expected SIM IP"
    else:
        result["verdict"] = "egress IP matches the account's expected SIM IP"

    return result


def classify_with_report(report: IpReport, expected_ip: str | None = None,
                         expected_geo: str | None = None) -> dict:
    """classify() plus ASN/org/cloud/carrier checks from a fetched IpReport."""
    result = classify(report.ip, expected_ip, expected_geo)
    if report.ip:
        asn_int = _as_int_asn(report.asn)
        result["asn"] = report.asn
        result["org"] = report.org
        result["country"] = report.country
        result["city"] = report.city
        if asn_int is not None:
            result["cloud"] = asn_int in _CLOUD_ASNS
            result["carrier"] = _carrier_name(report.country, asn_int)
            if result["cloud"]:
                result["warnings"].append(
                    f"ASN {asn_int} ({report.org or 'unknown'}) looks like a datacenter/cloud host — "
                    "not a consumer mobile carrier"
                )
            if expected_geo and (report.country or report.country_name):
                geo = str(expected_geo).strip().lower()
                cc = (report.country or "").strip().lower()
                cc_name = (report.country_name or "").strip().lower()
                city = (report.city or "").strip().lower()
                if not geo:
                    result["checks"]["geo_match"] = True
                elif (len(geo) == 2 and cc == geo) \
                    or (cc_name and geo in cc_name) \
                    or (cc and (geo in cc or cc in geo)) \
                    or (city and geo.split()[0] in city):
                    result["checks"]["geo_match"] = True
                else:
                    result["checks"]["geo_match"] = False
                    result["ok"] = False
                    result["warnings"].append(
                        f"declared geo {expected_geo!r} does not look like the actual "
                        f"country {report.country!r} / city {report.city!r}"
                    )
        if result["cloud"]:
            result["verdict"] = (
                "egress IP is a datacenter/cloud ASN (likely NOT a mobile SIM) — "
                + result["verdict"]
            )
    return result
