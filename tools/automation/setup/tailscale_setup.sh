#!/usr/bin/env bash
# Reach the phone's ADB over Tailscale (recommended; survives reboots/IP changes).
#
# TWO OPTIONS to connect instance -> phone (this script = option A):
#   A) Tailscale: instance + phone both join one tailnet; instance runs `adb connect <phone-tailnet-ip>:5555`.
#   B) SSH reverse tunnel (no extra app): see setup/ssh_tunnel_setup.sh.
# NEVER expose adb (5555) on a public IP.
#
# CRITICAL (SIM path): keep Tailscale SPLIT-TUNNEL (do NOT enable "Full Tunnel"/subnet routing).
# The phone's DEFAULT egress must stay its SIM so IG/TikTok see a real carrier IP; Tailscale is
# used ONLY for control (adb 5555, scrcpy). Verify with:  tools/verify_ip.py  (from the phone).
#
# Usage:  setup/tailscale_setup.sh [PHONE_TAILNET_IP_OR_NAME]     (idempotent)
set -euo pipefail
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
PHONE="${1:-${PHONE_TAILNET:-}}"

if ! command -v tailscale >/dev/null 2>&1; then
  echo ">> installing tailscale on the instance"
  curl -fsSL https://tailscale.com/install.sh | $SUDO sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo ">> authenticate this instance (open the printed URL, or set TS_AUTHKEY):"
  $SUDO tailscale up ${TS_AUTHKEY:+--authkey="$TS_AUTHKEY"} --hostname=model-hub-control
fi
echo ">> instance tailnet IP: $(tailscale ip -4 | head -1)"

cat <<'TXT'

== Phone side (one-time, manual) ==
 1. Install the Tailscale app on the phone, sign in to the SAME tailnet, keep it "Always-on VPN"
    (Android Settings > Network > VPN > Tailscale > Always-on) and exempt it from battery optimisation.
 2. Enable Developer options > USB debugging, and plug the phone into any computer by USB ONCE:
        adb tcpip 5555         # switches adbd to TCP; unplug afterwards
    (Android 11+ alternative: Developer options > Wireless debugging; port differs and is re-randomised -
     prefer `adb tcpip 5555` + setup/adb_boot_survival.sh.)
 3. Note the phone's tailnet IP (Tailscale app, or `tailscale status` here) -> put it in config.yaml
    devices[].serial / adb_addr as "100.x.y.z:5555".
 4. Restrict who may reach tcp/5555 with a Tailscale ACL (only the instance).
TXT

if [ -n "$PHONE" ]; then
  echo ">> adb connect $PHONE:5555"
  adb start-server >/dev/null
  adb connect "$PHONE:5555"
  adb devices
  echo ">> keepalive: install setup/adb-keepalive.service (documented in README)"
fi
