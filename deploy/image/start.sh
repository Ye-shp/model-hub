#!/bin/sh
# Keeps the image server and its tunnel running. Nothing listens on a public port.
set -u
[ "${#IMAGE_API_KEY}" -ge 32 ] || { echo "IMAGE_API_KEY must be set (at least 32 characters)"; exit 1; }
[ "${#TUNNEL_TOKEN}" -ge 32 ] || { echo "TUNNEL_TOKEN must be set (this box's own Cloudflare tunnel)"; exit 1; }
nvidia-smi --query-gpu=index,name,memory.total --format=csv,noheader 2>/dev/null || true

keep() {  # name, command...: restart with a short backoff if it exits
  name=$1; shift
  while true; do
    echo "[start] $name"
    "$@"
    echo "[start] $name exited ($?); restarting in 10 s"
    sleep 10
  done
}

# The tunnel gets only its token; the server never sees it.
# HTTP/2 over TCP instead of the default QUIC: during a long render the QUIC connection hit
# "timeout: no recent network activity" and dropped the request (seen on the live box).
keep cloudflared env -i PATH="$PATH" HOME=/root TUNNEL_TOKEN="$TUNNEL_TOKEN" /usr/bin/cloudflared tunnel --no-autoupdate --protocol "${TUNNEL_PROTOCOL:-http2}" run &
unset TUNNEL_TOKEN  # the background tunnel loop already has its copy
keep image-server python /opt/image/image_server.py
