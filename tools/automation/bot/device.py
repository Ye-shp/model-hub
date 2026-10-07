"""DeviceController: adb over the tunnel, lazy uiautomator2 session, scrcpy, screenshots.

DRY-RUN: nothing touches adb/the phone; intended actions are only logged. uiautomator2 is imported lazily.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

log = logging.getLogger("bot.device")


class DeviceError(RuntimeError):
    pass


class DeviceController:
    def __init__(
        self,
        serial: str,
        adb_addr: str | None = None,
        *,
        dry_run: bool = True,
        adb_bin: str = "adb",
        scrcpy_bin: str = "scrcpy",
        scrcpy_args: Sequence[str] = ("--no-audio", "--no-playback"),
        screenshots_dir: Path | str = "screenshots",
        runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
    ):
        self.serial = serial
        self.adb_addr = adb_addr or serial
        self.dry_run = dry_run
        self.adb_bin, self.scrcpy_bin = adb_bin, scrcpy_bin
        self.scrcpy_args = list(scrcpy_args)
        self.screenshots_dir = Path(screenshots_dir)
        self._run = runner
        self._u2: Any = None
        self._scrcpy: subprocess.Popen | None = None

    # ------------------------------------------------------------------ adb
    def _adb(self, *args: str, serial: bool = True, timeout: float = 30, text: bool = True) -> subprocess.CompletedProcess:
        cmd = [self.adb_bin] + (["-s", self.serial] if serial else []) + list(args)
        try:
            return self._run(cmd, capture_output=True, text=text, timeout=timeout)
        except FileNotFoundError as e:
            raise DeviceError(f"adb binary not found ({self.adb_bin}); run setup/instance_setup.sh") from e
        except subprocess.TimeoutExpired as e:
            raise DeviceError(f"adb timed out: {' '.join(cmd)}") from e

    def connect(self) -> bool:
        """`adb connect host:port` (tunnel endpoint). Idempotent."""
        if self.dry_run:
            log.info("[dry-run] adb connect %s", self.adb_addr)
            return True
        r = self._adb("connect", self.adb_addr, serial=False, timeout=15)
        out = (r.stdout or "") + (r.stderr or "")
        ok = "connected" in out.lower() and "cannot" not in out.lower() and "failed" not in out.lower()
        log.info("adb connect %s -> %s", self.adb_addr, out.strip())
        return ok

    def is_connected(self) -> bool:
        """True only if adb lists the serial as `device` (real check, even in dry-run; False if adb missing)."""
        try:
            r = self._adb("devices", serial=False, timeout=10)
        except DeviceError:
            return False
        for line in (r.stdout or "").splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[0] == self.serial and parts[1] == "device":
                return True
        return False

    def shell(self, cmd: str) -> str:
        if self.dry_run:
            log.info("[dry-run] adb shell %s", cmd)
            return ""
        r = self._adb("shell", cmd)
        if r.returncode != 0:
            raise DeviceError(f"adb shell failed ({cmd}): {r.stderr.strip()}")
        return r.stdout.strip()

    def egress_ip(self) -> str | None:
        """The public IP the phone egresses through right now (what IG/TikTok see). None if undetectable."""
        if self.dry_run:
            log.info("[dry-run] egress IP check")
            return None
        for cmd in ("curl -s --max-time 10 ipinfo.io/ip", "curl -s --max-time 10 icanhazip.com"):
            try:
                out = self._adb("shell", cmd, timeout=15).stdout.strip().splitlines()
                ip = out[-1].strip() if out else ""
                if ip and ip[0].isdigit() and ip.count(".") == 3:
                    return ip
            except DeviceError:
                continue
        return None

    def read_timezone(self) -> str:
        return self._adb("shell", "getprop persist.sys.timezone").stdout.strip()

    def read_locale(self) -> str:
        return self._adb("shell", "getprop persist.sys.locale").stdout.strip()

    def push_file(self, local: Path, remote_dir: str = "/sdcard/DCIM/Camera/") -> str:
        """Copy an asset to the phone gallery and trigger a media scan so pickers show it as newest."""
        remote = remote_dir.rstrip("/") + "/" + Path(local).name
        if self.dry_run:
            log.info("[dry-run] adb push %s %s (+media scan)", local, remote)
            return remote
        r = self._adb("push", str(local), remote, timeout=300)
        if r.returncode != 0:
            raise DeviceError(f"adb push failed: {r.stderr.strip()}")
        self._adb("shell", "am", "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                  "-d", f"file://{remote}")
        return remote

    def screenshot(self, name: str) -> Path | None:
        if self.dry_run:
            log.info("[dry-run] screenshot %s", name)
            return None
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        r = self._adb("exec-out", "screencap", "-p", text=False, timeout=20)
        if r.returncode != 0 or not r.stdout:
            raise DeviceError("screencap failed")
        path = self.screenshots_dir / f"{self.serial.replace(':', '_')}_{name}.png"
        path.write_bytes(r.stdout)
        return path

    # ------------------------------------------------------------------ uiautomator2 (lazy)
    def session(self):
        """uiautomator2 Device. Imported lazily so dry-run / CLI work with no phone and no u2 installed."""
        if self.dry_run:
            raise DeviceError("session() is not available in dry-run (drivers skip live steps)")
        if self._u2 is None:
            try:
                import uiautomator2 as u2  # lazy
            except ImportError as e:
                raise DeviceError("uiautomator2 not installed: pip install -r requirements.txt") from e
            self._u2 = u2.connect(self.serial)
        return self._u2

    # ------------------------------------------------------------------ scrcpy
    def start_scrcpy(self, record_to: Path | None = None) -> bool:
        """Headless mirror (optionally recording) for verification/debugging. scrcpy is a binary, not pip."""
        args = [self.scrcpy_bin, "-s", self.serial, *self.scrcpy_args]
        if record_to:
            args += ["--record", str(record_to)]
        if self.dry_run:
            log.info("[dry-run] %s", " ".join(args))
            return True
        if shutil.which(self.scrcpy_bin) is None:
            raise DeviceError(f"scrcpy not found ({self.scrcpy_bin}); apt install scrcpy")
        self.stop_scrcpy()
        self._scrcpy = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True

    def stop_scrcpy(self) -> None:
        if self.dry_run:
            log.info("[dry-run] stop scrcpy")
            return
        if self._scrcpy and self._scrcpy.poll() is None:
            self._scrcpy.terminate()
            try:
                self._scrcpy.wait(5)
            except subprocess.TimeoutExpired:
                self._scrcpy.kill()
        self._scrcpy = None
