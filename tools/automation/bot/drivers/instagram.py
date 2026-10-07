"""Instagram native-app posting via uiautomator2.

!! CALIBRATION REQUIRED !!  The selectors below are best-effort heuristics (resource-id / content-desc / text) based
on common IG Android builds. Instagram ships A/B UI variants and rotates ids, so each selector list must be
verified ONCE per app version (use `python -m uiautomator2 ... ` / weditor / `d.dump_hierarchy()` over scrcpy) and the
lists below trimmed to what matches. Candidate lists are tried in order; the dry-run path never evaluates them.
"""
from __future__ import annotations

import time

from ..config import Platform
from ..content import PostJob
from .base import BaseDriver, PostResult, Selector, Step, VerifyFailed

PKG = "com.instagram.android"

# --- selector candidates (UNVERIFIED; calibrate once per app version) -------------------------------------------
CREATE_BTN: list[Selector] = [
    {"resourceId": f"{PKG}:id/creation_tab"},
    {"description": "Create"}, {"descriptionContains": "New post"}, {"description": "Create new post"},
]
MODE_REEL: list[Selector] = [{"text": "REEL"}, {"text": "Reel"}, {"description": "Reel"}]
MODE_POST: list[Selector] = [{"text": "POST"}, {"text": "Post"}]
FIRST_MEDIA: list[Selector] = [  # gallery grid; newest asset is first because push_file() triggers a media scan
    {"resourceId": f"{PKG}:id/gallery_grid_item_thumbnail"},
    {"resourceId": f"{PKG}:id/gallery_grid_item_selection_overlay"},
    {"className": "android.widget.CheckBox", "instance": 0},
]
NEXT_BTN: list[Selector] = [
    {"resourceId": f"{PKG}:id/next_button_textview"}, {"resourceId": f"{PKG}:id/clips_right_action_button"},
    {"description": "Next"}, {"text": "Next"},
]
CAPTION_FIELD: list[Selector] = [
    {"resourceId": f"{PKG}:id/caption_input_text_view"}, {"resourceId": f"{PKG}:id/caption_text_view"},
    {"textContains": "Write a caption"}, {"className": "android.widget.EditText"},
]
SHARE_BTN: list[Selector] = [
    {"resourceId": f"{PKG}:id/share_footer_button"}, {"resourceId": f"{PKG}:id/next_button_textview", "text": "Share"},
    {"text": "Share"}, {"description": "Share"},
]
UPLOADING: list[Selector] = [{"textContains": "Sharing"}, {"textContains": "Posting"}, {"textContains": "Uploading"}]
HOME_TAB: list[Selector] = [{"resourceId": f"{PKG}:id/feed_tab"}, {"description": "Home"}]
DISMISS: list[Selector] = [{"text": "Not now"}, {"text": "Not Now"}, {"text": "OK"}, {"text": "Skip"}]


class InstagramDriver(BaseDriver):
    platform = Platform.IG
    package = PKG

    def post(self, job: PostJob) -> PostResult:
        return self._execute(job, self._steps(job))

    def _steps(self, job: PostJob) -> list[Step]:
        reel = job.platform_tags.get("mode", "reel" if job.media_kind == "video" else "post") == "reel"
        return [
            (f"push asset to phone gallery ({job.asset_path.name})", lambda d: self.device.push_file(job.asset_path)),
            ("launch Instagram (native app)", lambda d: d.app_start(PKG, use_monkey=True)),
            ("wait for home feed", lambda d: self._wait_home(d)),
            ("browse feed like a person (scroll + dwell)", self.browse),
            ("tap Create (+)", lambda d: self.tap(d, CREATE_BTN)),
            (f"choose {'Reel' if reel else 'Post'} mode", lambda d: self._mode(d, reel)),
            ("select newest media", lambda d: self.tap(d, FIRST_MEDIA)),
            ("Next (editor)", lambda d: self.tap(d, NEXT_BTN)),
            ("Next (reel/post options)", lambda d: self._maybe_next(d)),
            (f"type caption + {len(job.hashtags)} hashtags", lambda d: self.type_into(d, CAPTION_FIELD, job.full_caption())),
            ("Share / publish", lambda d: self.tap(d, SHARE_BTN)),
            ("verify upload finished", lambda d: self._verify(d)),
            ("leave app (home)", self.go_home),
        ]

    # --- live-only helpers ------------------------------------------------------------------------------------
    def _wait_home(self, d) -> None:
        for _ in range(3):  # clear first-launch popups ("Turn on notifications" etc.)
            if self.exists_any(d, HOME_TAB, 4):
                return
            if self.exists_any(d, DISMISS, 1):
                self.tap(d, DISMISS)
        if not self.exists_any(d, HOME_TAB, 4):
            raise VerifyFailed("Instagram home tab not visible: logged out, update prompt, or selectors need calibration")

    def _mode(self, d, reel: bool) -> None:
        if self.exists_any(d, MODE_REEL if reel else MODE_POST, 2):
            self.tap(d, MODE_REEL if reel else MODE_POST)

    def _maybe_next(self, d) -> None:
        if self.exists_any(d, NEXT_BTN, 3):
            self.tap(d, NEXT_BTN)

    def _verify(self, d, timeout: float = 120.0) -> None:
        """Best effort: progress banner appears then disappears and we are back on a main tab.
        Stronger check (calibrate): open profile and compare post count / newest thumbnail."""
        end = time.monotonic() + timeout
        seen = False
        while time.monotonic() < end:
            busy = self.exists_any(d, UPLOADING, 1.5)
            seen = seen or busy
            if not busy and self.exists_any(d, HOME_TAB, 1.5):
                return
            self.sleep(3)
        raise VerifyFailed(f"upload did not settle in {timeout:.0f}s (banner seen={seen})")
