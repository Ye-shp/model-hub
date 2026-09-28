"""Controls one Android phone over USB with adb (Android platform-tools).

Phone setup, once: Settings > About phone > tap "Build number" 7 times, then
Settings > Developer options > turn on "USB debugging" (and, on Xiaomi phones,
"USB debugging (Security settings)"). Plug in, accept the prompt on the phone,
and check that `adb devices` lists it.
"""
from __future__ import annotations

import os
import random
import re
import subprocess
import time

APPS = {
    # TikTok ships under two package names depending on region.
    "tiktok": ["com.zhiliaoapp.musically", "com.ss.android.ugc.trill"],
    "instagram": ["com.instagram.android"],
}


class Phone:
    def __init__(self, adb: str | None = None, serial: str | None = None):
        self.adb = adb or os.environ.get("ADB", "adb")
        self.serial = serial or os.environ.get("PHONE_SERIAL") or None
        self._size: tuple[int, int] | None = None

    def _cmd(self, *args: str, binary: bool = False, timeout: int = 30):
        base = [self.adb] + (["-s", self.serial] if self.serial else [])
        out = subprocess.run(base + list(args), capture_output=True, timeout=timeout, check=False)
        if out.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args[:3])} failed: {out.stderr.decode(errors='ignore').strip()[:300]}")
        return out.stdout if binary else out.stdout.decode(errors="ignore")

    def shell(self, *args: str, timeout: int = 30) -> str:
        return self._cmd("shell", *args, timeout=timeout)

    # ---- state ----
    def check(self) -> str:
        devices = [l.split("\t")[0] for l in self._cmd("devices").splitlines()[1:] if l.strip().endswith("device")]
        if not devices:
            raise SystemExit("No phone found. Plug it in, allow USB debugging on the phone, then run: adb devices")
        if len(devices) > 1 and not self.serial:
            raise SystemExit(f"Several devices connected ({', '.join(devices)}). Set PHONE_SERIAL in agents/.env.")
        if self.serial and self.serial not in devices:
            raise RuntimeError("Selected device is unavailable or has not authorized adb")
        self.serial = self.serial or devices[0]
        return self.serial

    def size(self) -> tuple[int, int]:
        if not self._size:
            m = re.findall(r"(\d+)x(\d+)", self.shell("wm", "size"))
            w, h = map(int, m[-1])  # "Override size" (if any) is listed last
            self._size = (w, h)
        return self._size

    def screenshot(self) -> bytes:
        png = self._cmd("exec-out", "screencap", "-p", binary=True)
        if not png.startswith(b"\x89PNG"):
            raise RuntimeError("Screenshot failed (is the screen on and unlocked?)")
        return png

    def foreground(self) -> str:
        out = self.shell("dumpsys", "window")
        m = re.search(r"mCurrentFocus=.*?\s([\w.]+)/", out)
        return m.group(1) if m else ""

    def screen_text(self) -> str:
        """Visible text from the accessibility tree. Video feeds often refuse this; screenshots always work."""
        try:
            self.shell("uiautomator", "dump", "/sdcard/window.xml", timeout=15)
            xml = self.shell("cat", "/sdcard/window.xml")
        except Exception:
            return ""
        texts = re.findall(r'(?:text|content-desc)="([^"]{2,})"', xml)
        return "\n".join(dict.fromkeys(t.replace("&amp;", "&").replace("&quot;", '"') for t in texts))

    # ---- actions ----
    def open_app(self, app: str) -> None:
        installed = self.shell("pm", "list", "packages")
        for package in APPS[app]:
            if f"package:{package}" in installed:
                self.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
                time.sleep(4)
                return
        raise SystemExit(f"{app} is not installed on the phone.")

    def open_url(self, url: str) -> None:
        self.shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url)
        time.sleep(4)

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def swipe_next(self) -> None:
        """Human-like upward swipe to the next video, with small random variation."""
        w, h = self.size()
        x = int(w * random.uniform(0.4, 0.6))
        y1 = int(h * random.uniform(0.72, 0.80))
        y2 = int(h * random.uniform(0.18, 0.26))
        self.shell("input", "swipe", str(x), str(y1), str(x + random.randint(-30, 30)), str(y2), str(random.randint(220, 380)))

    def back(self) -> None:
        self.shell("input", "keyevent", "4")

    def home(self) -> None:
        self.shell("input", "keyevent", "3")

    def push_to_gallery(self, local_path: str) -> str:
        """Copy a finished video/image into the phone's gallery so it can be posted from the app."""
        name = os.path.basename(local_path)
        remote = f"/sdcard/DCIM/ModelHub/{name}"
        self.shell("mkdir", "-p", "/sdcard/DCIM/ModelHub")
        self._cmd("push", local_path, remote, timeout=300)
        self.shell("am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE", "-d", f"file://{remote}")
        return remote
