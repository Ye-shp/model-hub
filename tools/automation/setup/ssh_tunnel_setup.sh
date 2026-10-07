#!/usr/bin/env bash
# Alternative to Tailscale: SSH reverse tunnel so the instance sees the phone's adbd as localhost:5555.
#
# TWO OPTIONS (this script = option B; option A is setup/tailscale_setup.sh):
#   B) A small always-on machine NEXT TO THE PHONE (Raspberry Pi / laptop / the phone itself via Termux) holds the
#      phone's adb (USB or `adb tcpip 5555`) and opens:   ssh -R 5555:<phone>:5555 user@instance
#      The instance then runs `adb connect localhost:5555` (config.yaml device serial = "localhost:5555").
#
# Usage:
#   On the INSTANCE (once):     setup/ssh_tunnel_setup.sh instance
#   On the PHONE-SIDE HOST:     INSTANCE_HOST=<instance host/ip> INSTANCE_USER=<user> setup/ssh_tunnel_setup.sh phone
# Idempotent. Uses autossh when available so the tunnel self-heals.
set -euo pipefail
ROLE="${1:-}"
SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
PHONE_ADDR="${PHONE_ADDR:-localhost:5555}"      # where adbd listens from the phone-side host's view

case "$ROLE" in
instance)
  echo ">> instance: make sure sshd allows remote forwards bound to loopback (default). Checking sshd config..."
  grep -Eq '^\s*AllowTcpForwarding\s+no' /etc/ssh/sshd_config && echo "!! set AllowTcpForwarding yes in /etc/ssh/sshd_config" || true
  echo ">> create a dedicated key-only user/key for the tunnel (recommended), then add the phone-side host's public key to"
  echo "   ~/.ssh/authorized_keys with:  restrict,port-forwarding,permitlisten=\"127.0.0.1:5555\" ssh-ed25519 AAAA..."
  echo ">> after the tunnel is up:  adb connect localhost:5555   (bound to 127.0.0.1 only - never public)"
  ;;
phone)
  : "${INSTANCE_HOST:?set INSTANCE_HOST}"; : "${INSTANCE_USER:?set INSTANCE_USER}"
  KEY="$HOME/.ssh/id_ed25519_tunnel"
  [ -f "$KEY" ] || { ssh-keygen -t ed25519 -N "" -f "$KEY" -C "adb-tunnel"; echo ">> add $KEY.pub to the instance authorized_keys (see 'instance' role)"; }
  command -v autossh >/dev/null 2>&1 || $SUDO apt-get install -y autossh || true
  adb tcpip 5555 || echo "!! could not run 'adb tcpip 5555' (phone not attached via USB?) - skip if adbd is already on TCP"
  if command -v autossh >/dev/null 2>&1; then
    echo ">> starting autossh tunnel (foreground; wrap in systemd/tmux for permanence)"
    exec autossh -M 0 -N -i "$KEY" -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -o ExitOnForwardFailure=yes \
      -R "127.0.0.1:5555:${PHONE_ADDR}" "${INSTANCE_USER}@${INSTANCE_HOST}"
  else
    exec ssh -N -i "$KEY" -o ServerAliveInterval=30 -o ExitOnForwardFailure=yes -R "127.0.0.1:5555:${PHONE_ADDR}" "${INSTANCE_USER}@${INSTANCE_HOST}"
  fi
  ;;
*) echo "usage: $0 instance|phone"; exit 2;;
esac
