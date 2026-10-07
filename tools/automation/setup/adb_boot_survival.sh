#!/usr/bin/env bash
# Keep adb-over-network alive across phone reboots. BEST-EFFORT: Android deliberately resets `adb tcpip` on reboot.
# Run from any host that can currently reach the phone by USB or adb (idempotent).
#
# What works, by situation:
#  1. Rooted phone (or Magisk module):  persist `setprop persist.adb.tcp.port 5555` + restart adbd.  <- done below if root.
#  2. Un-rooted (recommended, keeps Play Integrity clean): after a reboot adbd is USB-only. Options:
#       - Android 11+ "Wireless debugging" (Developer options): stays enabled on known Wi-Fi, but the port is random ->
#         pair once (`adb pair`), and use `adb mdns services`; poor over a tailnet.
#       - Keep a tiny always-on USB host (Pi) next to the phone and run `adb tcpip 5555` from it on boot (systemd
#         unit calling this script with SERIAL=<usb serial>) -> then use the SSH reverse tunnel (setup/ssh_tunnel_setup.sh).
#  OEM caveats: Xiaomi/Huawei/Samsung aggressively kill background VPN apps (Tailscale) - disable battery optimisation,
#  lock the app in recents, enable autostart. "USB debugging (Security settings)" on MIUI is required for input injection.
set -euo pipefail
SERIAL="${SERIAL:-}"
A=(adb); [ -n "$SERIAL" ] && A=(adb -s "$SERIAL")

"${A[@]}" wait-for-device
if "${A[@]}" shell 'id -u' | grep -q '^0'; then
  echo ">> root available: persisting adb tcp port"
  "${A[@]}" shell 'setprop persist.adb.tcp.port 5555; stop adbd; start adbd'
else
  echo ">> no root: switching adbd to TCP for this boot session only"
  "${A[@]}" tcpip 5555
fi
echo ">> stay-awake while charging (reduces tunnel drops):"
"${A[@]}" shell settings put global stay_on_while_plugged_in 3 || true
echo ">> adbd TCP port now: $("${A[@]}" shell getprop service.adb.tcp.port 2>/dev/null | tr -d '\r')"
