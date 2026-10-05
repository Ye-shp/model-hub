#!/usr/bin/env bash
# setup.sh — build a self-contained Tor daemon (no sudo) from Ubuntu .debs.
# Downloads tor + libevent, extracts the binary/lib into ./bin and ./lib,
# creates the runtime dir, then starts the daemon and bootstraps.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$DIR"

TOR_VER="0.4.9.11-0ubuntu0.24.04.1"
EV_VER="2.1.12-stable-9ubuntu2.2"
MIRROR="http://archive.ubuntu.com/ubuntu"
SEC="http://security.ubuntu.com/ubuntu"

echo "==> checking tools"
for t in curl python3 dpkg-deb; do
  command -v "$t" >/dev/null 2>&1 || { echo "  missing: $t"; exit 1; }
done

echo "==> fetching tor ${TOR_VER} (amd64)"
curl -fsSL -o /tmp/tor.deb \
  "$MIRROR/pool/universe/t/tor/tor_${TOR_VER}_amd64.deb" \
  || curl -fsSL -o /tmp/tor.deb \
  "http://security.ubuntu.com/ubuntu/pool/universe/t/tor/tor_${TOR_VER}_amd64.deb"

echo "==> fetching libevent ${EV_VER} (amd64)"
curl -fsSL -o /tmp/libevent.deb \
  "$SEC/pool/main/libe/libevent/libevent-2.1-7t64_${EV_VER}_amd64.deb" \
  || curl -fsSL -o /tmp/libevent.deb \
  "$MIRROR/pool/main/libe/libevent/libevent-2.1-7t64_${EV_VER}_amd64.deb"

echo "==> extracting tor binary -> bin/"
rm -rf /tmp/_torx && mkdir -p /tmp/_torx
dpkg-deb -x /tmp/tor.deb /tmp/_torx
mkdir -p bin
# the binary lives at usr/bin/tor in the package
find /tmp/_torx -type f -name tor -path '*usr/bin/tor' -exec cp -f {} bin/tor \;
chmod +x bin/tor

echo "==> extracting libevent -> lib/"
rm -rf /tmp/_evx && mkdir -p /tmp/_evx
dpkg-deb -x /tmp/libevent.deb /tmp/_evx
mkdir -p lib
find /tmp/_evx -type f -name 'libevent-2.1.so.7*' -exec cp -f {} lib/ \;
ls -la lib/ | grep -q 'libevent-2.1.so.7' || { echo "  libevent .so not found"; exit 1; }

# Some tor builds need libevent's exact soname symlink present in lib/.
[ -f lib/libevent-2.1.so.7 ] || ln -sf libevent-2.1.so.7.0.1 lib/libevent-2.1.so.7 2>/dev/null || true

echo "==> creating run/ (data + log + pid)"
mkdir -p run

echo "==> starting tor"
./torctl.sh start

echo
echo "==> done. Verify with:"
echo "    curl -s --socks5-hostname 127.0.0.1:9151 --max-time 30 https://check.torproject.org/api/ip"
echo "    python3 fetch.py --json https://check.torproject.org/api/ip"
