"""Unit tests for the pure IP classifier core (bot.netverify).

These run without a phone: `classify` / `classify_with_report` are pure functions,
and `fetch_ip_report` takes an injectable `fetch` callable.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

from bot.netverify import (  # noqa: E402
    IpReport,
    classify,
    classify_with_report,
    fetch_ip_report,
)


def _report(ip, asn, org, country, city, country_name="United States"):
    return IpReport(endpoint="t", ip=ip, asn=asn, org=org, country=country,
                    city=city, country_name=country_name)


def test_expected_ip_match():
    r = classify("73.78.11.22", expected_ip="73.78.11.22")
    assert r["ok"] is True
    assert r["checks"]["expected_ip_match"] is True


def test_expected_ip_mismatch():
    r = classify("93.125.82.10", expected_ip="73.78.11.22")
    assert r["ok"] is False
    assert any("expected 73.78.11.22" in w for w in r["warnings"])


def test_no_expected_ip_is_ok():
    assert classify("1.2.3.4")["ok"] is True


def test_missing_ip_is_not_ok():
    r = classify("", expected_ip="1.2.3.4")
    assert r["ok"] is False
    assert "no IP" in r["verdict"]


def test_cloud_asn_flagged():
    rep = _report("54.184.212.10", "14618", "Amazon", "US", "N. Virginia", "United States")
    r = classify_with_report(rep, expected_ip="54.184.212.10")
    assert r["cloud"] is True
    assert any("datacenter" in w.lower() for w in r["warnings"])
    assert "datacenter" in r["verdict"].lower()


def test_carrier_asn_positive_signal():
    rep = _report("73.78.11.22", "21436", "T-Mobile USA", "US", "Ashburn")
    r = classify_with_report(rep, expected_ip="73.78.11.22")
    assert r["cloud"] is False
    assert r["carrier"] == "t-mobile"


def test_geo_iso2_match():
    rep = _report("73.78.11.22", "21436", "T-Mobile USA", "US", "Ashburn")
    assert classify_with_report(rep, expected_geo="US")["ok"] is True


def test_geo_name_match():
    rep = _report("73.78.11.22", "21436", "T-Mobile USA", "US", "Ashburn", "United States")
    assert classify_with_report(rep, expected_geo="United States")["ok"] is True


def test_geo_city_match():
    rep = _report("73.78.11.22", "21436", "T-Mobile USA", "US", "Ashburn")
    assert classify_with_report(rep, expected_geo="Ashburn")["ok"] is True


def test_geo_mismatch_fails():
    rep = _report("73.78.11.22", "21436", "T-Mobile USA", "US", "Ashburn")
    r = classify_with_report(rep, expected_geo="DE")
    assert r["ok"] is False
    assert any("does not look like" in w for w in r["warnings"])


def test_fetch_ip_report_pure_and_injectable():
    def fake_fetch(url, timeout):
        return json.dumps({"ip": "8.8.8.8", "asn": 15169, "org": "Google",
                           "country": "US", "city": "Mountain View",
                           "country_name": "United States"})
    rep = fetch_ip_report("ipinfo.io/json", fetch=fake_fetch)
    assert rep.ip == "8.8.8.8" and rep.asn == "15169" and rep.country_name == "United States"
    # Google ASN 15169 is a cloud host -> flagged
    assert classify_with_report(rep)["cloud"] is True


def test_fetch_ip_report_handles_failure():
    def broken_fetch(url, timeout):
        raise OSError("boom")
    rep = fetch_ip_report("ipinfo.io/json", fetch=broken_fetch)
    assert rep.ip is None and rep.asn is None
