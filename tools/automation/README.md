# automation/ — IG + TikTok posting control plane

Implements the design in `../automation-buildout.md`: a Python control plane on the rented instance
(`Ye-shp/model-hub`) drives the **native** Instagram/TikTok apps on a **real Android phone** through
uiautomator2 over an ADB tunnel (Tailscale or SSH reverse tunnel — never USB, never public ADB).

Identity model: **1 account ↔ 1 device ↔ 1 dedicated 4G/5G mobile IP ↔ 1 geo** (tz/lang/country).
This is for posting your own original content to accounts you own; it automates a legitimate UI flow but
is still automation — platform ToS can restrict it, and nothing here guarantees no flags.

## Architecture

```
 ┌──────────────────────── instance (Ye-shp/model-hub) ────────────────────────┐
 │  bot.cli ──► Orchestrator ──► cadence (ramp/caps/tz/jitter)                  │
 │                 │   │  └────► ContentPipeline (jobs.yaml → PostJob)          │
 │                 │   └───────► StateStore (state/runtime/*.json)              │
 │                 ├─► drivers/instagram.py | drivers/tiktok.py (uiautomator2)  │
 │                 └─► ShadowbanWatcher (TikTok hashtag check, IG Account Status)│
 │        DeviceController: adb / u2 session (lazy) / scrcpy / screenshots      │
 └───────────────────────────────▲──────────────────────────────────────────────┘
                                 │ Tailscale (100.x:5555)  or  ssh -R 5555 (localhost:5555)
 ┌───────────────────────────────┴──────────────────────────────────────────────┐
 │ Android phone: native IG + TikTok apps, adbd on TCP 5555, matched tz/lang    │
 └───────────────────────────────┬──────────────────────────────────────────────┘
                                 │ SIM / dedicated mobile IP (1 per account, geo-matched)
                              carrier
```

## Quickstart

```bash
cd automation
./setup/instance_setup.sh             # or: pip install -r requirements.txt  (dry-run needs only PyYAML/tzdata)
python -m bot.cli doctor              # checks python/adb/scrcpy/device/config/state (missing tools = WARN in dry-run)
python -m bot.cli status              # identity map, warm-up day, today's cap, health
python -m bot.cli queue               # planned schedule for pending jobs (account-local times)
python -m bot.cli post --account ig_main --dry-run
python tools/dry_run.py               # whole cycle, no phone
python -m pytest tests -q
```

Going live: pick ONE tunnel (`setup/tailscale_setup.sh` or `setup/ssh_tunnel_setup.sh`), copy
`config.example.yaml → config.yaml`, `state/accounts.example.yaml → state/accounts.yaml`,
`jobs/jobs.example.yaml → jobs/jobs.yaml`, set `dry_run: false`, log in to each app once by hand (scrcpy),
run `connect`, `doctor`, then `schedule` (or the `modelbot.service` unit). `--dry-run` / `--live` on any
command override the config. Dry-run **never writes** `state/runtime/`.

## Commands

| command | does |
|---|---|
| `doctor [--strict]` | environment + config/accounts/jobs validation; exit 1 only on real failures (`--strict`: warnings too) |
| `connect` | `adb connect` every configured device |
| `status` | per-account table: device, ip, geo, warm-up day, cap, posts today, health, pause |
| `queue` | planned slot per pending job (stable between polls), blocked jobs with reason |
| `post --account X [--job ID] [--dry-run] [--force]` | post next pending job now; live mode refuses if paused / over cap / outside window unless `--force` |
| `watch [--account X]` | run the shadowban/account-status watcher; WARNING/SHADOWBANNED pauses the account |
| `schedule [--poll S] [--once]` | loop: post due jobs, run watcher every `interval_hours` |
| `verify-ip [--account X] [--expected-ip IP] [--geo US]` | prove the phone egresses through the SIM: fetch the live egress IP, classify ASN/carrier/geo (flags a datacenter/cloud ASN), compare to the account's `ip`+`geo` |

## Config reference (`config.yaml`)

| key | meaning |
|---|---|
| `dry_run` | global safety switch (default true) |
| `strict_one_account_per_device` | true: max 1 account per phone; false: 1 per (device, platform) — e.g. 1 IG + 1 TikTok sharing the phone's IP/geo |
| `devices[]` | `serial`, `adb_addr` (tailnet `100.x:5555` or `localhost:5555`), `label` |
| `instance.*` | state/accounts/jobs paths (fall back to `*.example.yaml`), binaries, scrcpy args, app packages, `reencode_tiktok` |
| `cadence.window_start/end` | account-local posting window (default 08:00–22:00) |
| `cadence.ramp_days` | days 1–7 use `week1[]`; 8…ramp_days interpolate to `steady` |
| `cadence.platforms.{ig,tiktok}` | `week1` (7 non-decreasing caps), `steady` (IG 4 / TikTok 6), `hard_max` (IG 5 / TikTok 8; never exceeded) |
| `cadence.delays` | lognormal inter-action (median 3 s, clamp 1.5–6 s) and inter-post gap (median 60 min, clamp 20–240 min) |
| `cadence.top_of_hour_guard_min` | no posts within ±N min of :00 (default 3) |
| `cadence.max_retries/retry_backoff_s/pause_after_failures` | exponential backoff; N consecutive failed jobs → 24 h pause |
| `watcher.*` | `interval_hours`, `check_after_post`, `pause_days` (default 7), TikTok probe hashtag fallback |

`state/accounts.yaml`: `id, platform(ig|tiktok), username, device_serial, ip, geo{tz,lang,country}, warmup_day, health`.
`warmup_day` = today's warm-up day when the account is first seen; it then auto-advances (stored as a start date).
`jobs.yaml`: `id, account, platform, asset_path, caption, hashtags (IG 3–5, TikTok 1–5), post_time (optional, "not before", naive = account-local), platform_tags`.

**Validation (fails `doctor`/`status`/`post`)**: duplicate account id; duplicate account per (device, platform) (or per device when strict);
missing/invalid ip; invalid tz; tz not in the declared country; device not in config; accounts on one phone with different ip/geo;
one IP shared across devices; job → unknown account / platform mismatch / bad hashtag count / missing asset (warning in dry-run).

## What's REAL vs best-effort

**REAL (implemented, runs and is tested without a phone)**
- Tunnel setup scripts + systemd units (`setup/`; the live tunnel itself needs your phone/tailnet to verify).
- Config/accounts/jobs loading and identity validation; JSON state with atomic writes.
- Cadence engine: lognormal delays, warm-up ramp, per-platform hard caps, account-local window + DST-correct tz, :00 avoidance, min gaps (`tests/`).
- Orchestrator: planning, gating, retry/backoff, pause-on-failure, pause-on-warning, per-account state/run log.
- CLI, dry-run end-to-end (`tools/dry_run.py`), `DeviceController` (adb connect/devices/push/screencap, lazy uiautomator2, scrcpy).
- **Egress-IP guard + `verify-ip`**: pre-flight check that the phone's live egress IP matches the account's SIM IP (`Orchestrator._check_egress`, `verify_ip: warn|hard`), plus a pure, injectable IP/ASN/geo classifier (`bot/netverify.py`, `tools/verify_ip.py`) that flags datacenter/cloud ASNs — all unit-tested without a phone.
- Watcher *classifiers* (`classify_ig_status`, `classify_tiktok_visibility`) as pure functions.

**BEST-EFFORT — needs one-time calibration on the live apps (never executed against a real phone here)**
- Exact UI selectors in `drivers/instagram.py` and `drivers/tiktok.py` (resource-ids/text/content-desc candidates, tried in order; IG/TikTok rotate them and A/B-test layouts).
- Watcher screen navigation (IG Settings → Account status; TikTok search → Videos) and the keyword lists that read the result.
- Post verification (progress-banner-disappears heuristic; a profile-thumbnail check would be stronger).
- `adb_boot_survival.sh` / `adb-keepalive.service`: Android resets `adb tcpip` on reboot; OEM battery managers kill VPN apps.

**Needs live calibration first:** (1) IG + TikTok selectors per installed app version, (2) watcher navigation + status strings, (3) the phone's persistent tunnel/ADB-after-reboot behaviour and phone tz/lang matching the account geo (`doctor` compares tz when connected).

## Cost
Dedicated 4G/5G mobile IP ≈ **$2/IP/month** (flat) to ~$2+/GB; a real SIM per phone is the low-risk alternative. One IP per account, geo-matched to the account; do not rotate mid-session. Tailscale personal tier is free.

## Operating notes
- Warm up new accounts 3–7 days organically (manually) before enabling automation; keep the phone un-rooted for Play Integrity.
- TikTok: original content only; `reencode_tiktok` just re-encodes/strips metadata, it does **not** remove foreign watermarks. Keep hashtags few.
- The watcher's in-app TikTok check is weaker than a logged-out check; a miss is a WARNING ("verify manually"), which pauses the account for `pause_days`.
- Runtime files (`state/runtime/`, `config.yaml`, `state/accounts.yaml`) are git-ignored.
