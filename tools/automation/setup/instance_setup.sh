#!/usr/bin/env bash
# Control-instance bootstrap (Debian/Ubuntu). Idempotent: safe to re-run.
# Installs adb + scrcpy (+ffmpeg), creates a venv, installs requirements, creates state dirs.
# Phone reachability is a separate step -- pick ONE:
#   Tailscale (recommended)  -> setup/tailscale_setup.sh
#   SSH reverse tunnel       -> setup/ssh_tunnel_setup.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"

need=()
for b in adb scrcpy ffmpeg; do command -v "$b" >/dev/null 2>&1 || need+=("$b"); done
if [ "${#need[@]}" -gt 0 ]; then
  echo ">> installing missing: ${need[*]}"
  $SUDO apt-get update -y
  pkgs=(); for b in "${need[@]}"; do case $b in adb) pkgs+=(adb);; *) pkgs+=("$b");; esac; done
  $SUDO apt-get install -y python3 python3-venv python3-pip "${pkgs[@]}" \
    || echo "!! some packages failed (scrcpy may be missing on older Ubuntu: see https://github.com/Genymobile/scrcpy/blob/master/doc/linux.md)"
fi

[ -d "$HERE/.venv" ] || python3 -m venv "$HERE/.venv"
"$HERE/.venv/bin/pip" install -q --upgrade pip
"$HERE/.venv/bin/pip" install -q -r "$HERE/requirements.txt"

mkdir -p "$HERE/state/runtime" "$HERE/screenshots"
[ -f "$HERE/config.yaml" ] || cp "$HERE/config.example.yaml" "$HERE/config.yaml"
[ -f "$HERE/state/accounts.yaml" ] || cp "$HERE/state/accounts.example.yaml" "$HERE/state/accounts.yaml"
[ -f "$HERE/jobs/jobs.yaml" ] || cp "$HERE/jobs/jobs.example.yaml" "$HERE/jobs/jobs.yaml"

echo ">> done. Edit config.yaml, state/accounts.yaml, jobs/jobs.yaml, then:"
echo "   cd $HERE && .venv/bin/python -m bot.cli doctor"
