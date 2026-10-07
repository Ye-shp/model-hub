"""ShadowbanWatcher: best-effort health reads. Classifiers are pure (tested); screen navigation is UNVERIFIED and
needs one-time calibration. Dry-run returns a simulated OK without touching a phone.

* TikTok: search the account's most recent hashtag in-app, scan Videos results for the account's username.
  (The most reliable shadowban test is logged-OUT; an in-app check on the logged-in phone is a weaker proxy,
  so a miss is reported as WARNING = "inconclusive, verify manually", not as a hard ban.)
* Instagram: Settings -> Account Status screen; text is classified by keyword.
"""
from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from .config import Account, Health, Platform, WatcherCfg
from .device import DeviceController

log = logging.getLogger("bot.watcher")

IG_BAD_HARD = ("can't be recommended", "cannot be recommended", "isn't eligible", "not eligible", "not being recommended")
IG_BAD_SOFT = ("limited", "restricted", "violat", "removed", "disabled", "reduced", "your account may be")
IG_GOOD = ("eligible to be recommended", "is eligible", "good standing", "no issues", "all good", "hasn't violated", "nothing to")


@dataclass
class WatchResult:
    account_id: str
    platform: Platform
    health: Health
    detail: str
    simulated: bool = False
    checked_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


def classify_ig_status(texts: Iterable[str]) -> tuple[Health, str]:
    blob = " | ".join(t.lower() for t in texts if t)
    for k in IG_BAD_HARD:
        if k in blob:
            return Health.SHADOWBANNED, f"Account Status: '{k}'"
    for k in IG_BAD_SOFT:
        if k in blob:
            return Health.WARNING, f"Account Status mentions '{k}'"
    for k in IG_GOOD:
        if k in blob:
            return Health.OK, "Account Status: eligible/good standing"
    return Health.UNKNOWN, "Account Status screen not recognised (calibrate)"


def classify_tiktok_visibility(found: bool) -> tuple[Health, str]:
    if found:
        return Health.OK, "own video visible in hashtag results"
    return Health.WARNING, "own video NOT visible in hashtag results (inconclusive; verify logged-out)"


def ui_texts(xml_dump: str) -> list[str]:
    out: list[str] = []
    for n in ET.fromstring(xml_dump).iter("node"):
        for k in ("text", "content-desc"):
            if n.get(k):
                out.append(n.get(k))
    return out


class ShadowbanWatcher:
    def __init__(self, cfg: WatcherCfg, *, dry_run: bool = True, instagram_package: str = "com.instagram.android",
                 tiktok_package: str = "com.zhiliaoapp.musically"):
        self.cfg, self.dry_run = cfg, dry_run
        self.ig_pkg, self.tt_pkg = instagram_package, tiktok_package

    def check(self, account: Account, device: DeviceController, probe_hashtag: str | None = None) -> WatchResult:
        if self.dry_run:
            log.info("[dry-run] watcher: would check %s (%s)", account.id, account.platform.value)
            return WatchResult(account.id, account.platform, Health.OK, "simulated (dry-run)", simulated=True)
        try:
            if account.platform is Platform.TIKTOK:
                h, detail = self._tiktok(account, device, probe_hashtag or self.cfg.tiktok_probe_hashtag)
            else:
                h, detail = self._instagram(device)
        except Exception as e:  # noqa: BLE001 - a failed read must never look like "healthy"
            log.error("watcher failed for %s: %s", account.id, e)
            return WatchResult(account.id, account.platform, Health.UNKNOWN, f"check failed: {e}")
        return WatchResult(account.id, account.platform, h, detail)

    # ---- UNVERIFIED navigation: calibrate per app version -------------------------------------------------
    def _instagram(self, device: DeviceController) -> tuple[Health, str]:
        d = device.session()
        d.app_start(self.ig_pkg, use_monkey=True)
        d(description="Profile").click(timeout=8)                       # profile tab (content-desc varies)
        d(description="Options").click(timeout=8)                       # hamburger menu
        d(textContains="Account status").click(timeout=8)               # Settings -> Account status
        d.sleep(2)
        status = classify_ig_status(ui_texts(d.dump_hierarchy()))
        d.press("home")
        return status

    def _tiktok(self, account: Account, device: DeviceController, hashtag: str) -> tuple[Health, str]:
        d = device.session()
        d.app_start(self.tt_pkg, use_monkey=True)
        d(description="Search").click(timeout=8)                        # search icon (top right)
        d.send_keys(f"#{hashtag}", clear=True)
        d.press("enter")
        d(text="Videos").click(timeout=8)                               # results tab
        found = False
        for _ in range(self.cfg.tiktok_scan_swipes + 1):
            if account.username.lower() in " ".join(ui_texts(d.dump_hierarchy())).lower():
                found = True
                break
            w, h = d.window_size()
            d.swipe(w * 0.5, h * 0.75, w * 0.5, h * 0.3, duration=0.3)
            d.sleep(2)
        d.press("home")
        return classify_tiktok_visibility(found)
