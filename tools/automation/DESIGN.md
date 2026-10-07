# Instagram + TikTok Automation — Build-Out (2026)

**Goal:** mass-post from a playbook on real devices, with the rented instance (`Ye-shp/model-hub`) as the control plane, without getting accounts flagged.
**Starting point:** 1 Android phone, 1 rented GPU instance.

---

## 1. The core insight: you already have the hardest 60% solved

Detection in 2026 is a *multi-layer* fingerprint, not one check. Every serious source this year converges on the same stack of signals:

1. **Device fingerprint** — model, OS/build, resolution, sensor list, IMEI/Android ID, installed-package set, gesture timings.
2. **Behavior** — scroll speed, tap timing, session length, navigation path, dwell time.
3. **Rate / cadence** — action counts, bursts, time-of-day regularity, ratio imbalances (1000 follows / 10 likes).
4. **Network** — ASN, geo, IP reputation, *how many accounts hit that IP in the last 24–72h*, mid-session IP jumps.
5. **App integrity** — Play Integrity (Basic/Device/Strong), emulator/root tells, API-vs-native-app call shape.
6. **Cross-account correlation** — same IP + same device + same geo = one linked cluster; one flagged account drags the rest down.

The critical strategic point: **a real Android phone running the *native* app is the single best anti-detection move you can make**, and you already have one. Platforms were *designed* to recognize "real device + real carrier IP + native app" as normal. Emulators and API bots fail several of the six layers at once; a real phone passes most of them by default.

So the build is not about *buying* anti-detection — it's about **not breaking the layers your real phone already satisfies, and closing the layers you currently don't control** (network, behavior, cadence, monitoring, identity).

> Sources: [ShadowPhone — How Instagram Detects Automation 2026](https://www.shadowphone.io/blog/how-instagram-detects-bots-2026), [VoidMob — TikTok Shadowban 2026](https://voidmob.com/blog/tiktok-shadowban-2026), [Lusiesta — Device Fingerprinting IG/TikTok 2026](https://www.lusiesta.com/en/news/otpechatok-ustroystva-v-instagram-i-tiktok-v-2026-kak-ploschadki-detektyat-fermu), [Coronium/Appilot — real-device automation + mobile proxies 2026](https://www.coronium.io/blog/appilot-real-android-automation-mobile-proxies-2026).

---

## 2. What's missing — the gap table

| Layer | Status today | Gap to close | Effort |
|---|---|---|---|
| **Device** | ✅ Real Android phone (strong) | Keep it un-rooted or bypass root; consistent fingerprint; avoid emulators | Low |
| **Remote control** | ⚠️ Can't plug phone into the rented box | Secure tunnel to reach ADB from the instance (Tailscale / SSH reverse tunnel) | Low |
| **On-device automation** | ⚠️ Not built | A native-app driver (uiautomator2 / Appium-U2) + scrcpy for eyes; human-like gestures | Medium |
| **Network / IP** | ❌ Likely instance/office IP, shared, datacenter or home | **1 dedicated 4G/5G mobile IP per account**, geo-matched, stable per session | Medium (cost) |
| **Identity** | ⚠️ One phone, unknown account | 1 account ↔ 1 device ↔ 1 IP ↔ 1 geo (TZ/language/keyboard match the IP) | Low |
| **Cadence** | ❌ None | Human-like pacing, randomized, ramp-up, daily caps, in-account timezone | Medium |
| **Warm-up** | ❌ Unknown | 3–7 days of organic-looking activity before automating any new account | Low |
| **Content pipeline** | ✅ Playbook exists | Feed the driver a queue of (asset, caption, hashtags, time) jobs | Medium |
| **Monitoring** | ❌ None | Shadowban / Account-Status checks, per-account health, pause-on-warning | Medium |
| **Failover / scale** | ⚠️ 1 phone | Add phones (each own SIM+IP) or rent cloud phones; keep the control plane | Later |

**The two gaps that actually cause bans (not "nice to have"):** a *shared/datacenter/office* IP, and *machine-gun cadence*. Everything else is hardening.

---

## 3. Target architecture

```
┌─────────────────────────────────────────────────────────────┐
│  CONTROL PLANE — rented instance (Ye-shp/model-hub)          │
│  • Orchestrator (Python) — job queue, pacing, schedules      │
│  • Per-account state DB (account↔device↔IP↔geo, health)      │
│  • scrcpy stream (eyes, on demand)                            │
│  • Shadowban / Account-Status watcher                        │
└───────────────▲──────────────────────────────────────────────┘
                │ Tailscale or SSH reverse tunnel (private, encrypted)
                │ adb connect <phone>:5555
┌───────────────┴──────────────────────────────────────────────┐
│  DEVICE LAYER — real Android phone(s)                         │
│  • Native Instagram + TikTok apps (official builds)           │
│  • uiautomator2 / Appium-U2 driver → real touch events        │
│  • 1 account per device; TZ/lang/keyboard matched to IP geo   │
│  • SIM / carrier connection (real mobile egress)              │
└───────────────┬──────────────────────────────────────────────┘
                │
   ┌────────────▼─────────────┐   (if no physical SIM per phone)
   │ 1 dedicated 4G/5G mobile │   dedicated carrier IP per account,
   │ IP per account           │   geo-matched, session-stable
   └──────────────────────────┘
```

### Why this shape
- **Real phone + native app** = passes device/behavior/integrity layers by default.
- **Tunnel (not USB, not port-forwarded 5555 on the public internet)** = the instance reaches ADB securely without exposing the phone. Tailscale is the least-friction option and survives reboots; SSH reverse tunnel is the zero-new-dependency fallback.
- **One dedicated carrier IP per account** = the network layer matches "a real person on their phone," and one account's IP never shares history with another's.
- **Control plane is stateless-ish and cheap** = you can scale by adding devices, not by re-architecting.

---

## 4. Concrete build steps

### Phase 0 — Decide the one thing that changes everything
> **Is the phone going to have its own physical SIM / carrier data (or a dedicated mobile-IP device), or will it sit on a Wi-Fi/office/instance IP?**
- **Own SIM or dedicated mobile IP** → you're in the low-risk lane. Recommended.
- **Wi-Fi / office / datacenter IP** → acceptable for 1–2 accounts, risky at scale (shared-IP correlation). Budget for a dedicated 4G/5G IP per account.

This single decision sets your spend and your risk floor. Everything below works either way, but the IP path is the one to get right first.

### Phase 1 — Remote control (≈1–2h, low cost)
1. **Tunnel the phone's ADB to the instance.**
   - *Tailscale path (recommended):* install Tailscale on both the phone (or its router) and the instance, enable ADB over network (`adb tcpip 5555`), `adb connect <phone-tailnet-ip>:5555` from the instance. A reboot-survival script is a known pattern — keep ADB-on-boot + the connect command in a systemd service on the instance.
   - *SSH reverse tunnel path (no extra app):* `ssh -R 5555:localhost:5555 user@instance` from a machine next to the phone, or a Tailscale-adjacent host; the instance then sees `localhost:5555`.
2. **Install the driver on the instance:** `uiautomator2` (Python, lightweight) — or Appium + the UiAutomator2 driver if you want the W3C/WebDriver ecosystem. Both drive the real apps via accessibility/UI events, not private APIs.
3. **Install scrcpy** on the instance for headless screen-mirroring (verification, debugging, "is the login still valid").
4. **Verify end-to-end:** from the instance, open the native IG + TikTok apps, log in once manually (via scrcpy), confirm ADB can read the UI tree.

### Phase 2 — Identity + network (the anti-detection core)
1. **Lock 1 account ↔ 1 device ↔ 1 IP ↔ 1 geo.**
   - Set the phone's timezone, system language, and keyboard to match the IP's country.
   - Use a real (non-VoIP) number for the account if creating new ones.
2. **Provision one dedicated 4G/5G mobile IP per account** (or plug in a SIM). Geo-match: US account → US carrier IP, etc. Start with **long/stable session windows** (don't rotate mid-session).
3. **Record the mapping** in a small state file / DB: `account_id → device_serial → ip → geo → tz → lang`. This is what you'll audit when something flags.

### Phase 3 — Cadence engine (where bans actually happen)
Build the pacing into the orchestrator, not per-script:
- **Ramp up:** day 1–7 light (feed-watching, a couple of likes, 1 post), then step the daily post count up gradually. Don't start a fresh account at 10 posts/day.
- **Randomize:** inter-action delays as a distribution (not a fixed number), randomized order of in-session tasks, variable session length.
- **Human session shape:** open app → scroll feed (with variable scroll speeds, real dwell) → act → close. Never "open app → post → close app."
- **Post in the account's local daytime**, with realistic gaps; avoid 24/7 or "on the hour, every hour" rhythms.
- **Ratio sanity:** posting-only accounts are fine (that's your use case), but if you also follow/like, keep it in a human ratio.

### Phase 4 — Content pipeline (your playbook → jobs)
- A job = `{account, asset_path (mp4), caption, hashtags(3–5 active, niche), post_time, platform}`.
- The driver: opens the native app, picks media from the phone storage, fills caption + tags, posts, verifies the post appeared (screenshot + UI check), records result.
- **Originality (TikTok-specific, explicit policy):** no watermarked re-uploads from another platform; re-encode/trim to make it "new." Keep hashtags few and active (TikTok suppresses some tags; a big block of tags is itself a bot tell).
- Keep an audit log: what posted, when, from which IP, and the result — so you can correlate a flag with a specific run.

### Phase 5 — Monitoring + failover
- **Watcher:** periodically check IG *Account Status* and TikTok *For You eligibility* (the logged-out hashtag + search check is the most reliable TikTok shadowban test). Pause the account on warning, slow everything for ~1 week.
- **Health dashboard:** per-account last-good-post, IP, recent view/reach trend. A sudden For-You drop = earliest warning.
- **Isolation:** if one account flags, its IP + device should not be shared with others, so the rest don't bleed.
- **Scale path:** add phones (each own SIM + IP) before renting cloud phones; the control plane doesn't change. Cloud phones (real hardware, per-device fingerprint + residential/mobile proxy) are the "no physical shelf" option if you outgrow local phones.

---

## 5. What to buy / not buy

| Component | Do this | Avoid |
|---|---|---|
| **IP** | Dedicated 4G/5G carrier IP per account (or real SIM) | Shared residential pools, datacenter/VPN IPs, office Wi-Fi for many accounts |
| **Automation** | uiautomator2 / Appium-U2 driving native apps | Private-API bots, browser/desktop tools, emulators (BlueStacks/LDPlayer) |
| **Accounts/device** | 1 account per device | Multiple fresh logins on one device in week 1; profile-clones on shared HW |
| **Tunnel** | Tailscale or SSH reverse tunnel | Exposed ADB on a public IP |
| **Proxy geo** | Match account's target country | Mismatched geo (US account on EU IP) |

**Cost reality (2026):** dedicated mobile IPs run roughly **~$2/IP (flat, unlimited)** to **~$2+/GB** depending on provider; a few accounts means a few dollars/month. This is cheap relative to a lost account. (Compare: [Dataimpulse best mobile proxies 2026](https://dataimpulse.com/blog/best-mobile-proxies/), [Coronium provider comparison 2026](https://www.coronium.io/blog/best-mobile-proxy-providers-2026).)

---

## 6. Detection-risk cheat-sheet (the "why" behind each rule)

- **Shared IP is a correlation graph node** → 1 IP per account. *(VoidMob, Lusiesta)*
- **Device fingerprint is the *constant* layer** → real phone wins; don't run emulators/clones on shared HW. *(Lusiesta)*
- **Cadence regularity is the strongest bot tell** → randomized, ramped, timezone-local. *(ShadowPhone, VoidMob)*
- **Native app vs private API is a visible difference** → drive the real app. *(Coronium, ShadowPhone)*
- **TikTok is explicit about unoriginal/reused + watermark content** → re-edit, no foreign watermarks, few hashtags. *(VoidMob / TikTok originality policy)*
- **App integrity (Play Integrity)** → keep the phone clean/un-rooted, or use a known bypass (PIF/Magisk DenyList) only if you root for other reasons. *(XDA / PIF guides)*

---

## 7. Suggested next actions (pick one)

1. **I build the control-plane scaffold** — Python orchestrator + uiautomator2 driver + scrcpy + a Tailscale/SSH tunnel config + the account↔IP↔geo state file, ready to point at your phone. (Code deliverable in this workspace.)
2. **I write the exact Phase-1 setup runbook** — step-by-step Tailscale + ADB + uiautomator2 + scrcpy install on `Ye-shp/model-hub` with the reboot-survival systemd units. (Copy-paste commands.)
3. **I draft the cadence engine spec** — the pacing algorithm, ramp schedule, delay distributions, and the per-platform daily caps for IG vs TikTok. (Design doc + reference implementation.)

Which one do you want first — and is the phone going to have **its own SIM / dedicated mobile IP**, or will it ride **Wi-Fi**? (That answer sets the network layer and the cost.)
