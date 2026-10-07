import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import Account, Geo, Health, Platform, load_config, validate_accounts  # noqa: E402

CFG = load_config()


def acc(id="a", plat=Platform.IG, dev="100.101.102.103:5555", ip="203.0.113.10", tz="America/New_York", country="US"):
    return Account(id, plat, "u", dev, ip, Geo(tz, "en-US", country), 1, Health.OK)


def errs(*accounts):
    return validate_accounts(list(accounts), CFG).errors


def test_valid_pair_on_one_phone():
    assert errs(acc("a"), acc("b", Platform.TIKTOK)) == []


def test_duplicate_platform_per_device():
    assert any("duplicate ig" in e for e in errs(acc("a"), acc("b")))


def test_missing_ip():
    assert any("missing ip" in e for e in errs(acc(ip="")))


def test_geo_tz_mismatch():
    assert any("geo mismatch" in e for e in errs(acc(tz="Europe/Berlin", country="US")))


def test_unknown_device_and_shared_ip():
    e = errs(acc("a", dev="nope:1"))
    assert any("not listed" in x for x in e)


def test_strict_mode():
    CFG.strict_one_account_per_device = True
    try:
        assert any("strict" in e for e in errs(acc("a"), acc("b", Platform.TIKTOK)))
    finally:
        CFG.strict_one_account_per_device = False


def test_job_unknown_account(tmp_path):
    from bot.content import ContentPipeline
    f = tmp_path / "j.yaml"
    f.write_text("jobs:\n  - {id: x, account: ghost, platform: ig, asset_path: a.mp4, caption: c, hashtags: [a,b,c]}\n")
    jobs, v = ContentPipeline(f, {"a": acc("a")}, allow_missing_assets=True).load()
    assert not jobs and any("unknown account" in e for e in v.errors)
