"""Config + account dataclasses, YAML loaders and identity validation.

Identity rule (from automation-buildout.md): 1 account <-> 1 device <-> 1 dedicated mobile IP <-> 1 geo.
"""
from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field, fields
from datetime import time
from enum import Enum
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

ROOT = Path(__file__).resolve().parent.parent  # the automation/ directory


class Platform(str, Enum):
    IG = "ig"
    TIKTOK = "tiktok"


class Health(str, Enum):
    OK = "ok"
    WARNING = "warning"
    SHADOWBANNED = "shadowbanned"
    PAUSED = "paused"
    UNKNOWN = "unknown"


class ConfigError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


@dataclass
class Validation:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "Validation") -> None:
        self.errors += other.errors
        self.warnings += other.warnings

    def raise_if_errors(self) -> None:
        if self.errors:
            raise ConfigError(self.errors)


# ----------------------------------------------------------------------------- config dataclasses
@dataclass
class DeviceCfg:
    serial: str
    adb_addr: str = ""
    label: str = ""

    def __post_init__(self) -> None:
        self.adb_addr = self.adb_addr or self.serial


@dataclass
class InstanceCfg:
    name: str = "Ye-shp/model-hub"
    state_dir: str = "state"
    accounts_file: str = "state/accounts.yaml"
    jobs_file: str = "jobs/jobs.yaml"
    screenshots_dir: str = "screenshots"
    adb_bin: str = "adb"
    scrcpy_bin: str = "scrcpy"
    scrcpy_args: list[str] = field(default_factory=lambda: ["--no-audio", "--no-playback"])
    ffmpeg_bin: str = "ffmpeg"
    reencode_tiktok: bool = True
    instagram_package: str = "com.instagram.android"
    tiktok_package: str = "com.zhiliaoapp.musically"
    log_level: str = "INFO"
    # Pre-flight egress-IP guard (live posts only): "warn" logs when the phone's SIM IP
    # differs from account.ip; "hard" blocks the post; "off" skips the check.
    verify_ip: str = "warn"


@dataclass
class DelayCfg:
    action_median_s: float = 3.0
    action_sigma: float = 0.45
    action_min_s: float = 1.5
    action_max_s: float = 6.0
    gap_median_min: float = 60.0
    gap_sigma: float = 0.5
    gap_min_min: float = 20.0
    gap_max_min: float = 240.0


@dataclass
class PlatformCadence:
    week1: list[int]
    steady: int
    hard_max: int


def _default_platforms() -> dict[str, PlatformCadence]:
    return {
        "ig": PlatformCadence(week1=[1, 1, 1, 1, 2, 2, 2], steady=4, hard_max=5),
        "tiktok": PlatformCadence(week1=[1, 1, 1, 2, 2, 2, 2], steady=6, hard_max=8),
    }


@dataclass
class CadenceCfg:
    window_start: str = "08:00"
    window_end: str = "22:00"
    ramp_days: int = 14
    top_of_hour_guard_min: int = 3
    delays: DelayCfg = field(default_factory=DelayCfg)
    platforms: dict[str, PlatformCadence] = field(default_factory=_default_platforms)
    browse_swipes: list[int] = field(default_factory=lambda: [3, 6])
    max_retries: int = 2
    retry_backoff_s: float = 30.0
    pause_after_failures: int = 3


@dataclass
class WatcherCfg:
    enabled: bool = True
    interval_hours: float = 12.0
    check_after_post: bool = True
    pause_days: int = 7
    tiktok_probe_hashtag: str = "fyp"
    tiktok_scan_swipes: int = 4


@dataclass
class MediaPrepCfg:
    """Anti-AI-detection media pipeline (see bot/mediaprep.py + bot/aifinger.py).

    Applied to every video before it is pushed to the phone. `enabled: false` falls back
    to the old minimal re-encode. Watermark regions are in pixels of the *source* frame
    (x,y,w,h) and are clamped to the real frame size automatically.
    """
    enabled: bool = True
    ffmpeg_bin: str = "ffmpeg"
    ffprobe_bin: str = "ffprobe"
    # 1. provenance + container
    strip_c2pa: bool = True
    strip_metadata: bool = True
    encoder_tag: str = "Lavf60.3.100"          # replace the encoder tag (hide the real tool)
    # 2. watermark removal (delogo). Empty regions + remove_watermark=True => default bottom-right box.
    remove_watermark: bool = False
    watermark_regions: list[dict] = field(default_factory=list)
    # 3. humanisation (subtle, breaks the "perfect AI" signal)
    humanize: bool = True
    contrast_shift: float = 0.01
    brightness_shift: float = 0.005
    grain_strength: int = 2
    sharpen: bool = True
    vignette: bool = True
    # 4. audio
    normalize_loudness: bool = True
    loudness_I: float = -14.0                  # target integrated LUFS (IG/TT default loudness)
    loudness_TP: float = -1.5
    loudness_LRA: float = 11.0
    highpass_hz: int = 80
    # 5. output encode
    crf: int = 19
    preset: str = "veryfast"
    audio_bitrate: str = "192k"
    target_fps: float = 30.0        # resample to 30 fps (phone default); breaks the 24 fps AI signature. 0 = keep source fps.
    pix_fmt: str = "yuv420p"
    # output location (relative to base_dir)
    output_dir: str = "state/media_prepped"
    workdir: str = "state/media_work"
    scan_on_prepare: bool = True               # run the AI-finger scan before + after


@dataclass
class Config:
    dry_run: bool = True
    strict_one_account_per_device: bool = False
    devices: list[DeviceCfg] = field(default_factory=list)
    instance: InstanceCfg = field(default_factory=InstanceCfg)
    cadence: CadenceCfg = field(default_factory=CadenceCfg)
    watcher: WatcherCfg = field(default_factory=WatcherCfg)
    mediaprep: MediaPrepCfg = field(default_factory=MediaPrepCfg)
    source: Path | None = None
    base_dir: Path = ROOT

    def device(self, serial: str) -> DeviceCfg | None:
        return next((d for d in self.devices if d.serial == serial), None)

    def path(self, rel: str) -> Path:
        p = Path(rel)
        return p if p.is_absolute() else self.base_dir / p

    @property
    def runtime_dir(self) -> Path:
        return self.path(self.instance.state_dir) / "runtime"


def parse_hhmm(s: str) -> time:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(s).strip())
    if not m or int(m[1]) > 23 or int(m[2]) > 59:
        raise ConfigError([f"bad HH:MM time {s!r}"])
    return time(int(m[1]), int(m[2]))


# ----------------------------------------------------------------------------- loading
def resolve_with_example(path: Path) -> Path:
    """`x.yaml` if present, else sibling `x.example.yaml` (keeps a fresh checkout dry-run safe)."""
    if path.exists():
        return path
    ex = path.with_name(path.stem + ".example" + path.suffix)
    if ex.exists():
        return ex
    raise ConfigError([f"file not found: {path} (nor {ex.name})"])


def _read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError([f"{path}: invalid YAML: {e}"]) from e
    if not isinstance(data, dict):
        raise ConfigError([f"{path}: top level must be a mapping"])
    return data


def _build(cls: type, data: Any, where: str):
    data = dict(data or {})
    names = {f.name for f in fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ConfigError([f"{where}: unknown keys {sorted(unknown)}"])
    try:
        return cls(**data)
    except TypeError as e:
        raise ConfigError([f"{where}: {e}"]) from e


def load_config(path: str | Path | None = None) -> Config:
    p = resolve_with_example(Path(path) if path else ROOT / "config.yaml")
    raw = _read_yaml(p)
    known = {"dry_run", "strict_one_account_per_device", "devices", "instance", "cadence", "watcher", "mediaprep"}
    if set(raw) - known:
        raise ConfigError([f"{p.name}: unknown top-level keys {sorted(set(raw) - known)}"])
    cad_raw = dict(raw.get("cadence") or {})
    delays = _build(DelayCfg, cad_raw.pop("delays", None), "cadence.delays")
    plats_raw = cad_raw.pop("platforms", None)
    cadence = _build(CadenceCfg, cad_raw, "cadence")
    cadence.delays = delays
    if plats_raw:
        cadence.platforms = {k: _build(PlatformCadence, v, f"cadence.platforms.{k}") for k, v in plats_raw.items()}
    cfg = Config(
        dry_run=bool(raw.get("dry_run", True)),
        strict_one_account_per_device=bool(raw.get("strict_one_account_per_device", False)),
        devices=[_build(DeviceCfg, d, "devices[]") for d in raw.get("devices") or []],
        instance=_build(InstanceCfg, raw.get("instance"), "instance"),
        cadence=cadence,
        watcher=_build(WatcherCfg, raw.get("watcher"), "watcher"),
          mediaprep=_build(MediaPrepCfg, raw.get("mediaprep"), "mediaprep"),
        source=p,
    )
    validate_config(cfg).raise_if_errors()
    return cfg


def validate_config(cfg: Config) -> Validation:
    v = Validation()
    if not cfg.devices:
        v.errors.append("config: no devices defined")
    serials = [d.serial for d in cfg.devices]
    for s in {s for s in serials if serials.count(s) > 1}:
        v.errors.append(f"config: duplicate device serial {s}")
    c = cfg.cadence
    try:
        if parse_hhmm(c.window_start) >= parse_hhmm(c.window_end):
            v.errors.append("cadence: window_start must be before window_end")
    except ConfigError as e:
        v.errors += e.errors
    if c.ramp_days <= 7:
        v.errors.append("cadence.ramp_days must be > 7")
    for name in cfg.cadence.platforms:
        if name not in {p.value for p in Platform}:
            v.errors.append(f"cadence.platforms: unknown platform {name!r}")
    for p in Platform:
        pc = c.platforms.get(p.value)
        if pc is None:
            v.errors.append(f"cadence.platforms.{p.value} missing")
            continue
        if len(pc.week1) != 7 or any(b < a for a, b in zip(pc.week1, pc.week1[1:])):
            v.errors.append(f"cadence.platforms.{p.value}.week1 must be 7 non-decreasing ints")
        elif not (pc.week1[-1] <= pc.steady <= pc.hard_max) or min(pc.week1) < 0:
            v.errors.append(f"cadence.platforms.{p.value}: need week1[-1] <= steady <= hard_max")
    d = c.delays
    if not (0 < d.action_min_s <= d.action_median_s <= d.action_max_s):
        v.errors.append("cadence.delays: need 0 < action_min_s <= action_median_s <= action_max_s")
    if not (0 < d.gap_min_min <= d.gap_median_min <= d.gap_max_min):
        v.errors.append("cadence.delays: need 0 < gap_min_min <= gap_median_min <= gap_max_min")
    if len(c.browse_swipes) != 2 or c.browse_swipes[0] > c.browse_swipes[1]:
        v.errors.append("cadence.browse_swipes must be [min, max]")
    return v


# ----------------------------------------------------------------------------- accounts
@dataclass
class Geo:
    tz: str
    lang: str
    country: str

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


@dataclass
class Account:
    id: str
    platform: Platform
    username: str
    device_serial: str
    ip: str
    geo: Geo
    warmup_day: int = 1
    health: Health = Health.UNKNOWN

    @property
    def tz(self) -> ZoneInfo:
        return self.geo.zone


# Sanity map: country -> allowed tz names/prefixes. Unlisted countries only produce a warning.
_US = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Phoenix",
       "America/Anchorage", "America/Detroit", "America/Boise", "America/Juneau", "America/Adak",
       "America/Indiana/", "America/Kentucky/", "Pacific/Honolulu")
COUNTRY_TZ: dict[str, tuple[str, ...]] = {
    "US": _US,
    "CA": ("America/Toronto", "America/Vancouver", "America/Edmonton", "America/Winnipeg", "America/Halifax",
           "America/St_Johns", "America/Regina"),
    "GB": ("Europe/London",), "DE": ("Europe/Berlin",), "FR": ("Europe/Paris",), "ES": ("Europe/Madrid", "Atlantic/Canary"),
    "IT": ("Europe/Rome",), "NL": ("Europe/Amsterdam",), "AU": ("Australia/",), "IN": ("Asia/Kolkata",),
    "JP": ("Asia/Tokyo",), "BR": ("America/Sao_Paulo", "America/Manaus", "America/Fortaleza", "America/Bahia",
                                  "America/Recife", "America/Belem", "America/Cuiaba", "America/Campo_Grande"),
    "MX": ("America/Mexico_City", "America/Monterrey", "America/Tijuana", "America/Cancun", "America/Merida"),
    "ID": ("Asia/Jakarta", "Asia/Makassar", "Asia/Jayapura", "Asia/Pontianak"),
    "PH": ("Asia/Manila",), "TR": ("Europe/Istanbul",), "AE": ("Asia/Dubai",),
}


def tz_matches_country(tz: str, country: str) -> bool | None:
    """True/False if we know the country, None if unknown."""
    allowed = COUNTRY_TZ.get(country.upper())
    if allowed is None:
        return None
    return any(tz == a or (a.endswith("/") and tz.startswith(a)) for a in allowed)


def _parse_account(raw: dict[str, Any], i: int) -> Account:
    where = f"accounts[{i}]({raw.get('id', '?')})"
    geo_raw = raw.get("geo") or {}
    try:
        geo = _build(Geo, geo_raw, where + ".geo")
    except ConfigError:
        geo = Geo(tz=str(geo_raw.get("tz", "")), lang=str(geo_raw.get("lang", "")), country=str(geo_raw.get("country", "")))
    try:
        platform = Platform(str(raw.get("platform", "")).lower())
    except ValueError:
        raise ConfigError([f"{where}: platform must be one of {[p.value for p in Platform]}"])
    try:
        health = Health(str(raw.get("health", "unknown")).lower())
    except ValueError:
        raise ConfigError([f"{where}: bad health {raw.get('health')!r}"])
    missing = [k for k in ("id", "username", "device_serial") if not raw.get(k)]
    if missing:
        raise ConfigError([f"{where}: missing {missing}"])
    return Account(
        id=str(raw["id"]), platform=platform, username=str(raw["username"]).lstrip("@"),
        device_serial=str(raw["device_serial"]), ip=str(raw.get("ip") or ""), geo=geo,
        warmup_day=int(raw.get("warmup_day", 1)), health=health,
    )


def load_accounts(path: str | Path) -> list[Account]:
    p = resolve_with_example(Path(path))
    raw = _read_yaml(p)
    items = raw.get("accounts")
    if not isinstance(items, list) or not items:
        raise ConfigError([f"{p.name}: 'accounts' must be a non-empty list"])
    return [_parse_account(r, i) for i, r in enumerate(items)]


def validate_accounts(accounts: list[Account], cfg: Config) -> Validation:
    v = Validation()
    ids = [a.id for a in accounts]
    for i in {i for i in ids if ids.count(i) > 1}:
        v.errors.append(f"duplicate account id {i}")
    for a in accounts:
        w = f"account {a.id}"
        if a.warmup_day < 1:
            v.errors.append(f"{w}: warmup_day must be >= 1")
        if not a.ip:
            v.errors.append(f"{w}: missing ip (1 account needs 1 dedicated mobile IP)")
        else:
            try:
                ipaddress.ip_address(a.ip)
            except ValueError:
                v.errors.append(f"{w}: invalid ip {a.ip!r}")
        if cfg.device(a.device_serial) is None:
            v.errors.append(f"{w}: device_serial {a.device_serial!r} not listed in config devices")
        try:
            ZoneInfo(a.geo.tz)
        except (ZoneInfoNotFoundError, ValueError, KeyError):
            v.errors.append(f"{w}: invalid timezone {a.geo.tz!r}")
        else:
            m = tz_matches_country(a.geo.tz, a.geo.country) if a.geo.country else False
            if not a.geo.country:
                v.errors.append(f"{w}: geo.country missing")
            elif m is False:
                v.errors.append(f"{w}: geo mismatch: tz {a.geo.tz} is not in country {a.geo.country}")
            elif m is None:
                v.warnings.append(f"{w}: country {a.geo.country} not in tz sanity map; tz/country not cross-checked")
        if not re.fullmatch(r"[a-z]{2,3}(-[A-Za-z]{2})?", a.geo.lang or ""):
            v.errors.append(f"{w}: geo.lang {a.geo.lang!r} should look like 'en-US'")
        elif "-" in a.geo.lang and a.geo.country and a.geo.lang.split("-")[1].upper() != a.geo.country.upper():
            v.warnings.append(f"{w}: lang {a.geo.lang} region differs from country {a.geo.country}")
        if a.health in (Health.PAUSED, Health.SHADOWBANNED):
            v.warnings.append(f"{w}: health={a.health.value}, it will not be posted to")

    by_dev: dict[str, list[Account]] = {}
    for a in accounts:
        by_dev.setdefault(a.device_serial, []).append(a)
    for serial, accs in by_dev.items():
        if cfg.strict_one_account_per_device and len(accs) > 1:
            v.errors.append(f"device {serial}: multiple accounts ({[a.id for a in accs]}) but strict_one_account_per_device=true")
        plats = [a.platform for a in accs]
        for p in set(plats):
            if plats.count(p) > 1:
                v.errors.append(f"device {serial}: duplicate {p.value} account on one device "
                                f"({[a.id for a in accs if a.platform == p]})")
        if len({a.ip for a in accs}) > 1:
            v.errors.append(f"device {serial}: accounts on one phone must share one ip, got {sorted({a.ip for a in accs})}")
        if len({(a.geo.tz, a.geo.country, a.geo.lang) for a in accs}) > 1:
            v.errors.append(f"device {serial}: accounts on one phone must share one geo (tz/country/lang)")
    by_ip: dict[str, set[str]] = {}
    for a in accounts:
        if a.ip:
            by_ip.setdefault(a.ip, set()).add(a.device_serial)
    for ip, devs in by_ip.items():
        if len(devs) > 1:
            v.errors.append(f"ip {ip} shared across devices {sorted(devs)} (cross-account correlation)")
    for d in cfg.devices:
        if d.serial not in by_dev:
            v.warnings.append(f"device {d.serial} has no accounts")
    return v
