"""Bounded batches across already-connected adb devices. No cloud-phone provisioning."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys


def plan(config: dict) -> list[list[str]]:
    devices = config.get("devices", [])
    if not isinstance(devices, list) or not 1 <= len(devices) <= 10:
        raise ValueError("Configure 1-10 devices")
    seen, commands = set(), []
    for item in devices:
        serial = item.get("serial", "")
        if not isinstance(serial, str) or not serial or serial in seen or len(serial) > 200:
            raise ValueError("Each device needs a unique adb serial")
        seen.add(serial)
        platform = item.get("platform", "tiktok")
        if platform not in {"tiktok", "instagram"}:
            raise ValueError("Unknown platform")
        commands.append([sys.executable, str(Path(__file__).with_name("collect.py")), platform,
                         "--serial", serial, "--project", str(item.get("project", "default")),
                         "--posts", str(item.get("posts", 20)), "--frames", str(item.get("frames", 3)),
                         "--seconds", str(item.get("seconds", 600))])
    return commands


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("config", type=Path)
    ap.add_argument("--workers", type=int, choices=[1, 2], default=2)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    commands = plan(json.loads(args.config.read_text()))
    if args.dry_run:
        print(json.dumps(commands, indent=2))
        return
    def execute(command):
        return subprocess.run(command, check=False).returncode
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        codes = list(executor.map(execute, commands))
    print(f"Finished {len(codes)} devices; {sum(c != 0 for c in codes)} failed.")
    raise SystemExit(1 if any(codes) else 0)


if __name__ == "__main__":
    main()
