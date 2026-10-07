"""TikTok native-app posting via uiautomator2.

!! CALIBRATION REQUIRED !!  Selectors are best-effort and UNVERIFIED. TikTok's UI changes often and differs by region
(package `com.zhiliaoapp.musically` global vs `com.ss.android.ugc.trill`). Calibrate once per app version by dumping
the hierarchy over scrcpy and trimming each candidate list. The dry-run path never evaluates them.

Originality: only upload your OWN original content. We re-encode/strip metadata (ffmpeg) so files are clean, but we do
NOT remove other platforms' watermarks -- watermarked/reused content is suppressed by TikTok policy. Keep hashtags
few (<=5) and active; a big tag block is itself a bot tell.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from ..config import Platform
from ..content import PostJob
from .base import BaseDriver, PostResult, Selector, Step, VerifyFailed

DEFAULT_PKG = "com.zhiliaoapp.musically"
log = logging.getLogger("bot.driver.tiktok")

CREATE_BTN: list[Selector] = [{"description": "Create"}, {"descriptionContains": "Create"}, {"resourceId": f"{DEFAULT_PKG}:id/ghf"}]
UPLOAD_BTN: list[Selector] = [{"text": "Upload"}, {"descriptionContains": "Upload"}]
FIRST_MEDIA: list[Selector] = [
    {"resourceId": f"{DEFAULT_PKG}:id/gallery_item"}, {"className": "android.widget.CheckBox", "instance": 0},
    {"descriptionContains": "video", "instance": 0},
]
NEXT_BTN: list[Selector] = [{"text": "Next"}, {"descriptionContains": "Next"}]
DESC_FIELD: list[Selector] = [
    {"textContains": "Describe your post"}, {"textContains": "Add description"}, {"className": "android.widget.EditText"},
]
POST_BTN: list[Selector] = [{"text": "Post"}, {"descriptionContains": "Post"}]
UPLOADING: list[Selector] = [{"textContains": "Uploading"}, {"textContains": "Posting"}, {"textContains": "Processing"}]
HOME_TAB: list[Selector] = [{"description": "Home"}, {"text": "Home"}]
DISMISS: list[Selector] = [{"text": "Not now"}, {"text": "Skip"}, {"text": "Got it"}, {"text": "Later"}]


def reencode_command(ffmpeg: str, src: Path, dst: Path) -> list[str]:
    """Clean re-encode: drop container metadata, normalise to H.264/AAC 1080x1920-friendly mp4."""
    return [ffmpeg, "-y", "-i", str(src), "-map_metadata", "-1", "-c:v", "libx264", "-preset", "veryfast",
            "-crf", "21", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(dst)]


class TikTokDriver(BaseDriver):
    platform = Platform.TIKTOK
    package = DEFAULT_PKG

    def __init__(self, *a, package: str = DEFAULT_PKG, reencode: bool = False, ffmpeg_bin: str = "ffmpeg", **kw):
        super().__init__(*a, **kw)
        self.package, self.reencode, self.ffmpeg_bin = package, reencode, ffmpeg_bin

    def post(self, job: PostJob) -> PostResult:
        return self._execute(job, self._steps(job))

    def _steps(self, job: PostJob) -> list[Step]:
        asset = {"p": job.asset_path}  # may be replaced by the re-encoded copy
        pkg = self.package
        return [
            ("re-encode + strip metadata (ffmpeg)" if self.reencode else "re-encode skipped (disabled)",
             lambda d: self._reencode(job, asset)),
            ("push asset to phone gallery", lambda d: self.device.push_file(asset["p"])),
            ("launch TikTok (native app)", lambda d: d.app_start(pkg, use_monkey=True)),
            ("wait for home", lambda d: self._wait_home(d)),
            ("browse For You like a person (scroll + dwell)", self.browse),
            ("tap Create (+)", lambda d: self.tap(d, CREATE_BTN)),
            ("tap Upload", lambda d: self.tap(d, UPLOAD_BTN)),
            ("select newest video", lambda d: self.tap(d, FIRST_MEDIA)),
            ("Next (selection)", lambda d: self.tap(d, NEXT_BTN)),
            ("Next (editor)", lambda d: self.tap(d, NEXT_BTN)),
            (f"type caption + {len(job.hashtags)} hashtags", lambda d: self.type_into(d, DESC_FIELD, job.full_caption())),
            ("Post / publish", lambda d: self.tap(d, POST_BTN)),
            ("verify upload finished", lambda d: self._verify(d)),
            ("leave app (home)", self.go_home),
        ]

    def _reencode(self, job: PostJob, asset: dict) -> None:
        if not self.reencode or job.media_kind != "video":
            return
        if shutil.which(self.ffmpeg_bin) is None:
            log.warning("ffmpeg not found; uploading original file")
            return
        out = Path(tempfile.mkdtemp(prefix="tt_")) / f"{job.job_id}.mp4"
        subprocess.run(reencode_command(self.ffmpeg_bin, job.asset_path, out), check=True, capture_output=True)
        asset["p"] = out

    def _wait_home(self, d) -> None:
        for _ in range(3):
            if self.exists_any(d, HOME_TAB, 4):
                return
            if self.exists_any(d, DISMISS, 1):
                self.tap(d, DISMISS)
        if not self.exists_any(d, HOME_TAB, 4):
            raise VerifyFailed("TikTok home not visible: logged out, popup, or selectors need calibration")

    def _verify(self, d, timeout: float = 180.0) -> None:
        """Best effort: upload banner appears then clears. Stronger (calibrate): open Profile and check newest thumbnail."""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self.exists_any(d, UPLOADING, 1.5) and self.exists_any(d, HOME_TAB, 1.5):
                return
            self.sleep(3)
        raise VerifyFailed(f"upload did not settle in {timeout:.0f}s")
