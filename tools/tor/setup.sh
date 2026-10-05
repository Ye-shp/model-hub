#!/usr/bin/env bash
# setup.sh — build a self-contained Tor daemon (no sudo) from Ubuntu .debs.
# Downloads authenticated tor + libevent packages into TOR_HOME (default: here).
# --install-only prepares a persistent runtime for the Hub's supervisor.
set -euo pipefail
umask 077
DIR="$(cd "$(dirname "$0")" && pwd)"
TOR_ROOT="${TOR_HOME:-$DIR}"
mkdir -p "$TOR_ROOT"
TOR_ROOT="$(cd "$TOR_ROOT" && pwd)"
cd "$TOR_ROOT"

case "${1:-}" in
  ""|--install-only) ;;
  *) echo "usage: bash setup.sh [--install-only]" >&2; exit 1 ;;
esac

TOR_VER="0.4.9.11-0ubuntu0.24.04.1"
EV_VER="2.1.12-stable-9ubuntu2.2"
# Pinned SHA256 values published by Ubuntu over HTTPS (verified 2026-10-05):
# https://packages.ubuntu.com/noble-updates/amd64/tor/download
# https://packages.ubuntu.com/noble-updates/amd64/libevent-2.1-7t64/download
TOR_SHA256="35818718981ab85e549c536278696f9821f84c62f9e3718dc5af174dfc37b428"
EV_SHA256="083ced6efb23476cb4de4e441b741af02db5653e67fa7fcd6cf7e5f7b78f6101"
MIRROR="https://archive.ubuntu.com/ubuntu"
SEC="https://security.ubuntu.com/ubuntu"
PINS="$TOR_VER $TOR_SHA256 $EV_VER $EV_SHA256"

echo "==> checking tools"
for t in curl python3 dpkg-deb sha256sum; do
  command -v "$t" >/dev/null 2>&1 || { echo "  missing: $t"; exit 1; }
done

if [ -x bin/tor ] && [ -f lib/libevent-2.1.so.7 ] \
  && [ "$(cat installed-packages 2>/dev/null || true)" = "$PINS" ] \
  && sha256sum --quiet -c installed-files.sha256 2>/dev/null; then
  echo "==> using verified persistent Tor installation"
else
  # Keep the verified packages and their extraction private until installation.
  WORK="$(mktemp -d "$TOR_ROOT/.install.XXXXXX")"
  trap 'rm -rf -- "$WORK"' EXIT
  CURL=(curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL --connect-timeout 15 --max-time 120)

  echo "==> fetching tor ${TOR_VER} (amd64)"
  "${CURL[@]}" -o "$WORK/tor.deb" \
    "$MIRROR/pool/universe/t/tor/tor_${TOR_VER}_amd64.deb" \
    || "${CURL[@]}" -o "$WORK/tor.deb" \
    "$SEC/pool/universe/t/tor/tor_${TOR_VER}_amd64.deb"

  echo "==> fetching libevent ${EV_VER} (amd64)"
  "${CURL[@]}" -o "$WORK/libevent.deb" \
    "$SEC/pool/main/libe/libevent/libevent-2.1-7t64_${EV_VER}_amd64.deb" \
    || "${CURL[@]}" -o "$WORK/libevent.deb" \
    "$MIRROR/pool/main/libe/libevent/libevent-2.1-7t64_${EV_VER}_amd64.deb"

  echo "==> verifying Ubuntu package checksums before extraction"
  printf '%s  %s\n' "$TOR_SHA256" "$WORK/tor.deb" "$EV_SHA256" "$WORK/libevent.deb" | sha256sum -c -

  echo "==> extracting tor binary -> bin/"
  dpkg-deb -x "$WORK/tor.deb" "$WORK/tor"
  mkdir -p bin
  cp -f "$WORK/tor/usr/bin/tor" bin/tor
  chmod +x bin/tor

  echo "==> extracting libevent -> lib/"
  dpkg-deb -x "$WORK/libevent.deb" "$WORK/libevent"
  mkdir -p lib
  find "$WORK/libevent" -type f -name 'libevent-2.1.so.7*' -exec cp -f {} lib/ \;
  [ -f lib/libevent-2.1.so.7 ] || ln -sf libevent-2.1.so.7.0.1 lib/libevent-2.1.so.7
  [ -f lib/libevent-2.1.so.7 ] || { echo "  libevent .so not found"; exit 1; }

  # Reuse the authenticated extraction after a container restart, checking its contents.
  sha256sum bin/tor lib/libevent-2.1.so.7* > installed-files.sha256
  printf '%s\n' "$PINS" > installed-packages
fi

echo "==> creating run/ (data + log + pid)"
mkdir -p run

if [ "${1:-}" = --install-only ]; then
  echo "==> Tor installed; foreground process is managed by the Hub supervisor"
  exit 0
fi

echo "==> starting tor"
# Code overlays do not preserve executable bits on scripts.
TOR_HOME="$TOR_ROOT" bash "$DIR/torctl.sh" start

echo
echo "==> done. Verify with:"
echo "    curl -s --socks5-hostname 127.0.0.1:9151 --max-time 30 https://check.torproject.org/api/ip"
echo "    python3 fetch.py --json https://check.torproject.org/api/ip"
