# SIM Setup Runbook — dedicated mobile IP for IG/TikTok automation (2026)

**Goal:** every account egresses over **its own real-carrier 4G/5G IP** (not the rented
instance's IP, not a datacenter/residential proxy, not Wi-Fi), geo-matched to the account's
target country, **stable within a session**. This is the single highest-leverage anti-detection
fix for the `Ye-shp/model-hub` control plane.

> **Read first — the one thing that trips everyone up:**
> Connecting a SIM to the phone is **not** enough. The phone must *route its default egress
> through that SIM* while *control (adb) goes through Tailscale*. If Tailscale is in "Full
> Tunnel" / subnet-routing mode, the apps egress through the **instance's IP** and you've lost
> the SIM entirely. The recipe in §4 keeps SIM = data, Tailscale = control only.
> **Prove it** with `automation/tools/verify_ip.py` before you post (§6).

---

## 1. Why a SIM (the "why" in one line)

IG/TikTok correlate accounts by **IP**. One IP = one graph node. A shared IP (home Wi-Fi,
office, datacenter, or a proxy pool) becomes a *correlation node* linking every account that
touched it → mass-flag risk. A **real consumer mobile IP** on a **real phone** with a
**real SIM** is the most "normal-looking" egress you can buy. *(VoidMob, Lusiesta, ShadowPhone.)*

## 2. What "SIM" means here (3 options)

| Option | What it is | Best for | Caveat |
|---|---|---|---|
| **Physical SIM** (recommended) | A prepaid/MVNO SIM card in the phone | Stable single-carrier IP, simplest | Needs a physical slot; 1 SIM per phone |
| **eSIM** (data-only) | A carrier eSIM profile (Airalo, Ubigi, Nomad…) | No slot, fast to buy | **Data-only (no phone number)**; some eSIMs are *multi-carrier* → can flip ASN; avoid flipping mid-session |
| **Mobile-proxy / phone-rental** (fallback) | Vendor phone or carrier-IP proxy pool | Many accounts fast | Cost per account; vendor dependency |

**Rule for this build:** **one SIM ↔ one phone ↔ one or two accounts (1 IG + 1 TikTok is fine;
2+ of the same platform on one IP is not).** Don't share a SIM/IP across two accounts on the
same platform.

## 3. Which providers actually work (2026)

> Verified from cited 2025/26 sources (see `sim-provider-research.md` for URLs + access dates);
> prices are indicative — confirm on the vendor page before you buy.

### 🇺🇸 United States — top picks
- **Tello or Mint Mobile (on T-Mobile)** — cheapest "real" consumer mobile IP. T-Mobile ASN
  `AS21928`, sticky-lease behavior (IP stable within a session — what we want).
  - Tello: from ~$5/3 GB, pay-per-month, no contract, eSIM + physical.
  - Mint Mobile: 3-month prepay, very cheap per GB.
- **If you specifically want a Verizon ASN:** **Visible** (~$25/mo) or **US Mobile (Verizon)**.
- **If you want an AT&T ASN:** **US Mobile (AT&T)**.
- **eSIM fallback (no physical slot):** **Airalo US** — 2026 US plans ride **AT&T / T-Mobile /
  Verizon**; pick the single-carrier SKU you want. Most predictable single-carrier eSIM.
- **Avoid:** SIMs that can flip T-Mobile↔AT&T *on the same account over time* (ASN drift) —
  pin one carrier.

### 🇬🇧 United Kingdom
- **Top pick:** **giffgaff (O2)** or **Voxi/Lebara (Vodafone)** — cheap rolling plans, real
  consumer mobile ASN (O2 `AS20940`, Vodafone `AS1273`).
- **Most "premium" ASN:** aim for **EE (BT, `AS6453`/`AS2856`)** or **Vodafone (`AS1273`)** if
  you can get a cheap line.
- **eSIM fallback:** aloSIM / GigSky UK (data-only; verify the actual UK carrier per SKU).

### 🇪🇺 EU (e.g. Germany)
- **Top pick:** a **local MVNO on the incumbent** — DE: **Congstar/AL so (Telekom)**, **Blau
  (Vodafone)**, **Freen (O2)**. Incumbent ASN is the "most real" in-country (`Telekom AS3320`,
  `Vodafone AS1273`, `Orange AS5511`, `Telefónica/O2 AS20940`).
- **Travel eSIMs** (Airalo et al.) often land on **Vodafone/O2**, not the incumbent — acceptable,
  but a local MVNO on the incumbent is the cleanest IP.

**Decision heuristic:** *host carrier (not the MVNO) determines the ASN.* Cheapest real line on
the **most-reused / most-trusted carrier in the account's country** = best balance.

---

## 4. The network topology (SIM = data, Tailscale = control)

```
                control (adb 5555 / scrcpy)                     data (IG & TikTok apps)
   Ye-shp/model-hub  ────────────────Tailscale 100.x.y.z──────────►  PHONE
   (control plane)      (tailnet only, SPLIT-TUNNEL)              │
                                                                  │  default egress
                                                                  ▼
                                                              SIM / carrier
                                                              real mobile IP
```

- **Tailscale = split-tunnel.** Both instance and phone join one tailnet; you reach the phone at
  `100.x.y.z:5555` for `adb`/`scrcpy`. **Do NOT enable "Full Tunnel" or subnet routing on the
  phone** — that would make the phone's app traffic leave via the tailnet/instance IP.
- **Phone default egress = the SIM.** Wi-Fi OFF (or set SIM as the preferred network). No VPN
  on the phone. No datacenter proxy.

### 4.1 Buy + install the SIM
1. Buy the chosen SIM (§3). For the US, **Tello/Mint (T-Mobile)** is the safe default.
2. Insert in the phone (or install the eSIM profile). Confirm **Mobile Data** shows the carrier
   with a real signal bar.
3. Set the phone to **Mobile data only**: Wi-Fi OFF. (Airplane mode + Mobile data ON is fine too.)
4. **Match locale + timezone to the carrier country** (Settings → System → Languages & time).
   The cadence engine schedules post windows in the account `geo.tz`; the phone's tz should match.
   The scaffold already reads `persist.sys.timezone` / `persist.sys.locale` for this.

### 4.2 Wire Tailscale for control (split-tunnel)
```bash
# on the PHONE: install Tailscale, join the tailnet, note its 100.x.y.z IP.
# on the INSTANCE:
automation/setup/tailscale_setup.sh <PHONE_TAILNET_IP>   # installs TS, auth-keys, adb connect
```
Then in `automation/config.yaml`, `devices[].serial` / `adb_addr` = `<PHONE_TAILNET_IP>:5555`.

> **Do not** route the phone's default traffic through the tailnet. Tailscale here is only the
> "wrench handle" for adb/scrcpy. The SIM is the "road" the apps drive on.

### 4.3 Prove the egress (the make-or-break check)
Run from the phone (or via `adb shell`) with **mobile data ON, Wi-Fi OFF, Tailscale in the
tailnet**:
```bash
automation/tools/verify_ip.py            # prints ip + ASN + geo for 3 endpoints
# or manually:  curl -s https://ipinfo.io/json   (from the phone)
```
Expected: a **real carrier ASN** for the account's country (e.g. `AS21928` T-Mobile for US),
**same IP across all three endpoints**, and a **geo that matches `geo.country`**.

> If you see the **instance's IP** (or a `100.x`/datacenter ASN), Tailscale is in full-tunnel —
> fix §4.2 before posting. If the IP **changes between endpoints**, the SIM is load-balancing —
> that's normal for some carriers; the important thing is it's a *real mobile ASN*, not the
> instance.

### 4.4 Record the IP in the identity map
Paste the real IP from §4.3 into `automation/state/accounts.yaml`:
```yaml
accounts:
  - id: ig_main
    platform: ig
    username: "your.ig.handle"
    device_serial: "100.x.y.z:5555"
    ip: "73.78.11.22"          # ← the SIM IP from verify_ip.py (one per account)
    geo: { tz: "America/New_York", lang: "en-US", country: "US" }
    warmup_day: 1
    health: unknown
```
`validate_accounts` enforces **non-empty `ip`** and (if `strict_one_account_per_device: true`)
one account per device. Keep each account's `ip` stable; never reuse one `ip` across two
accounts on the same platform.

### 4.5 The live pre-flight guard (already in the build)
The orchestrator now runs `_check_egress(account)` **before every live post**: it re-reads the
phone's current egress IP and compares it to `account.ip`. Mode = `instance.verify_ip`:
- `warn` (default) — logs a loud warning on mismatch (SIM off? Wi-Fi on? wrong SIM?) and posts.
- `hard` — **blocks** the post on mismatch (safest for first live runs; set this).
- `off` — skip the check.

So a silent "Wi-Fi came back on" can't quietly post the account through the wrong IP.

---

## 5. First post (safe sequence)

```bash
cd automation
python3 -m bot.cli doctor                 # all required checks pass
python3 -m bot.cli status                 # identity map + warm-up day look right
# capture + set the SIM IP:
python3 tools/verify_ip.py                # from the phone
# paste into state/accounts.yaml  (ip: <SIM IP>)
python3 -m bot.cli post --account ig_main --dry-run      # 13-step native-app flow, no real post
# flip to live (instance.verify_ip: "hard" for the first run):
python3 -m bot.cli post --account ig_main
```
First live run is still **calibrating the IG/TikTok button selectors** against *your* app
versions (see the buildout doc's "one-time UI calibration" item). Do one manual post by hand to
confirm the flow, then let the cadence engine take over.

## 6. Scaling to more accounts / phones

- **1 phone** → 1 SIM → **1 IG + 1 TikTok** (one IP shared by the two different platforms is OK;
  two accounts of the *same* platform on one IP is not).
- **More IG or TikTok accounts** → more **phones**, each with **its own SIM** (own IP), all
  reachable over the same tailnet. The control plane (`model-hub`) already manages many
  `devices[]` entries and routes each `account` to its `device_serial`.
- **Cost reality (2026):** a real consumer SIM is roughly **$5–25/mo per phone** (data-only,
  small allowance). That is *cheaper* than a lost account. eSIMs (Airalo etc.) are similar
  per-GB. Compare with the proxy route (~$2/IP flat to $2+/GB) — a SIM is the low-risk
  alternative for a small number of accounts.

## 7. Pitfalls checklist

- [ ] SIM is the **default egress** (Wi-Fi off) — not just "connected".
- [ ] Tailscale is **split-tunnel** (no full-tunnel / subnet routing) — control only.
- [ ] `verify_ip.py` shows a **real carrier ASN** for the account's country (not the instance IP).
- [ ] `accounts.yaml` `ip:` = that real IP; **unique per account**.
- [ ] Phone **locale + tz match** the account `geo` (cadence windows + fingerprint both care).
- [ ] **Don't rotate the SIM/IP mid-session**; don't flip carriers (ASN drift).
- [ ] First live run with `instance.verify_ip: "hard"` so a wrong-IP post is impossible.

---
*Sources: see `sim-provider-research.md` (36 URLs, verified/inferred split). Detection model:
`automation-buildout.md` §1 & §6. Code: `automation/bot/`, `automation/tools/verify_ip.py`,
`automation/setup/tailscale_setup.sh`.*
