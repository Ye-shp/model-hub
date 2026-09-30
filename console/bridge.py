"""Model Hub phone bridge: lets Qwen Cowork use the Android phone plugged into this PC.

Run it on the computer the phone is plugged into (Windows, macOS or Linux; Python 3.9+, no packages needed):

    python bridge.py --key phb_...            (the key is on the console's Phone page)

It needs adb (Android platform-tools) and a phone with USB debugging allowed. It only makes outgoing
HTTPS requests to your hub: it asks for work, runs it with adb, and sends back the result. Nothing listens
on this computer. Stop it with Ctrl+C.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import random
import re
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

VERSION = "1.0"
DEFAULT_URL = "https://api.handydandy.cc"
KEYS = {"4", "3", "66", "187", "67"}


class Phone:
    def __init__(self, adb: str, serial: str | None):
        self.adb, self.serial, self._size = adb, serial, None

    def run(self, *args: str, binary: bool = False, timeout: int = 30):
        base = [self.adb] + (["-s", self.serial] if self.serial else [])
        out = subprocess.run(base + list(args), capture_output=True, timeout=timeout, check=False)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.decode(errors="ignore").strip()[:300] or f"adb exited with {out.returncode}")
        return out.stdout if binary else out.stdout.decode(errors="ignore")

    def shell(self, *args: str, timeout: int = 30) -> str:
        return self.run("shell", *args, timeout=timeout)

    def devices(self) -> list[str]:
        out = subprocess.run([self.adb, "devices"], capture_output=True, text=True, timeout=15).stdout
        return [line.split("\t")[0] for line in out.splitlines()[1:] if line.strip().endswith("\tdevice")]

    def choose(self) -> str | None:
        found = self.devices()
        if self.serial:
            return self.serial if self.serial in found else None
        return found[0] if found else None

    def size(self) -> list[int] | None:
        if not self._size:
            found = re.findall(r"(\d+)x(\d+)", self.shell("wm", "size"))
            if found:
                self._size = [int(v) for v in found[-1]]  # an "Override size" is listed last
        return self._size

    def foreground(self) -> str:
        try:
            out = self.shell("dumpsys", "window")
        except RuntimeError:
            return ""
        match = re.search(r"mCurrentFocus=.*?\s([\w.]+)/", out) or re.search(r"mFocusedApp=.*?\s([\w.]+)/", out)
        return match.group(1) if match else ""

    def info(self) -> dict:
        return {"model": self.shell("getprop", "ro.product.model").strip(),
                "android": self.shell("getprop", "ro.build.version.release").strip(), "size": self.size()}

    # ---- commands from the hub ----
    def screenshot(self, **_):
        png = self.run("exec-out", "screencap", "-p", binary=True, timeout=40)
        if not png.startswith(b"\x89PNG"):
            raise RuntimeError("Screenshot failed (is the screen on and unlocked?)")
        return {"data": base64.b64encode(png).decode(), "size": self.size(), "foreground": self.foreground()}

    def ui(self, **_):
        self.shell("uiautomator", "dump", "/sdcard/window.xml", timeout=20)
        return {"data": self.shell("cat", "/sdcard/window.xml")}

    def tap(self, x: int, y: int, **_):
        self.shell("input", "tap", str(int(x)), str(int(y)))
        return {}

    def swipe(self, direction: str = "up", **_):
        w, h = self.size() or [1080, 2400]
        x = int(w * random.uniform(0.4, 0.6))
        y = int(h * random.uniform(0.45, 0.55))
        ms = str(random.randint(220, 380))
        if direction == "up":
            coords = (x, int(h * random.uniform(.72, .8)), x + random.randint(-30, 30), int(h * random.uniform(.18, .26)))
        elif direction == "down":
            coords = (x, int(h * random.uniform(.25, .3)), x + random.randint(-30, 30), int(h * random.uniform(.72, .8)))
        elif direction == "left":
            coords = (int(w * .85), y, int(w * .15), y + random.randint(-20, 20))
        elif direction == "right":
            coords = (int(w * .15), y, int(w * .85), y + random.randint(-20, 20))
        else:
            raise ValueError("Unknown direction")
        self.shell("input", "swipe", *map(str, coords), ms)
        return {}

    def text(self, text: str, **_):
        if not text.isascii():
            raise ValueError("adb can only type plain ASCII text")
        escaped = re.sub(r"([\\\"'`$&|;<>()*~?#!\[\]{}])", r"\\\1", text).replace(" ", "%s")
        self.shell("input", "text", escaped)
        return {}

    def key(self, key: str, **_):
        if key not in KEYS:
            raise ValueError("Key not allowed")
        self.shell("input", "keyevent", key)
        return {}

    def open_app(self, packages: list[str], **_):
        installed = self.shell("pm", "list", "packages")
        for package in packages:
            if re.fullmatch(r"[A-Za-z0-9_.]+", package) and f"package:{package}" in installed:
                self.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")
                return {"package": package}
        raise RuntimeError(f"Not installed: {', '.join(packages)}")

    def open_url(self, url: str, **_):
        if not re.fullmatch(r"https?://[^\s'\"`;&|<>]+", url):
            raise ValueError("Only plain http(s) links can be opened")
        self.shell("am", "start", "-a", "android.intent.action.VIEW", "-d", url)
        return {}


def request(url: str, key: str, path: str, body: dict, timeout: float) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url.rstrip("/") + path, data=data, method="POST", headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json", "User-Agent": f"ModelHubPhoneBridge/{VERSION}"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("HUB_BRIDGE_URL", DEFAULT_URL), help="your hub's API address")
    ap.add_argument("--key", default=os.environ.get("HUB_BRIDGE_KEY", ""), help="bridge key from the console's Phone page")
    ap.add_argument("--adb", default=os.environ.get("ADB") or shutil.which("adb") or "adb", help="path to adb")
    ap.add_argument("--serial", default=os.environ.get("PHONE_SERIAL"), help="which phone, if several are connected")
    args = ap.parse_args()
    if not args.key.startswith("phb_"):
        ap.error("Pass --key with the bridge key from the console's Phone page (it starts with phb_).")
    try:
        subprocess.run([args.adb, "version"], capture_output=True, timeout=15, check=True)
    except (OSError, subprocess.SubprocessError):
        sys.exit(f"adb not found at '{args.adb}'. Install Android platform-tools and pass --adb C:\\path\\to\\adb.exe")

    phone = Phone(args.adb, args.serial)
    handlers = {"screenshot": phone.screenshot, "ui": phone.ui, "tap": phone.tap, "swipe": phone.swipe,
                "text": phone.text, "key": phone.key, "open_app": phone.open_app, "open_url": phone.open_url,
                "info": lambda **_: phone.info()}
    print(f"Phone bridge {VERSION} → {args.url}   (Ctrl+C to stop)")
    details, backoff, said = {}, 2, ""
    while True:
        try:
            device = phone.choose()
            if device and device != phone.serial:
                phone.serial, phone._size = device, None
            if device and not details:
                details = phone.info()
            if not device:
                details = {}
            state = f"phone {device} ({details.get('model', '')})" if device else "no phone found (plug it in and allow USB debugging)"
            if state != said:
                print(time.strftime("%H:%M:%S"), state)
                said = state
            reply = request(args.url, args.key, "/bridge/poll", {
                "device": device, "devices": phone.devices(), "version": VERSION, "host": socket.gethostname(),
                "os": platform.system(), **details}, timeout=45)
            backoff = 2
            for command in reply.get("commands", []):
                started = time.time()
                try:
                    if not device:
                        raise RuntimeError("No phone is connected to the PC")
                    result = {"ok": True, **handlers[command["command"]](**(command.get("args") or {}))}
                except Exception as error:  # report every failure back instead of crashing
                    result = {"ok": False, "error": f"{type(error).__name__}: {error}"}
                print(time.strftime("%H:%M:%S"), command["command"], "ok" if result["ok"] else result["error"],
                      f"({time.time() - started:.1f}s)")
                request(args.url, args.key, "/bridge/result", {"id": command["id"], **result}, timeout=60)
        except KeyboardInterrupt:
            print("Stopped.")
            return
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                sys.exit("The hub refused the bridge key. Copy the current key from the console's Phone page.")
            print(time.strftime("%H:%M:%S"), f"hub answered HTTP {error.code}; retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, subprocess.SubprocessError) as error:
            print(time.strftime("%H:%M:%S"), f"connection problem ({type(error).__name__}); retrying in {backoff}s")
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)


if __name__ == "__main__":
    main()
