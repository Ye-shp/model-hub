#!/usr/bin/env bash
# torctl.sh — manage Tor in TOR_HOME/run (default: beside this file)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
TOR_ROOT="${TOR_HOME:-$DIR}"
mkdir -p "$TOR_ROOT/run"
TOR_ROOT="$(cd "$TOR_ROOT" && pwd)"
TOR="$TOR_ROOT/bin/tor"
PIDF="$TOR_ROOT/run/tor.pid"
export LD_LIBRARY_PATH="$TOR_ROOT/lib"

# Absolute runtime paths passed on the command line (RunAsDaemon needs absolute).
CFG_ARGS=(
  -f "$DIR/torrc"
  --DataDirectory "$TOR_ROOT/run"
  --PidFile "$PIDF"
  --Log "notice file $TOR_ROOT/run/tor.log"
)
# Hub mode exposes only its private Unix socket; standalone torrc stays unchanged.
if [ -n "${TOR_SOCKS_SOCKET:-}" ]; then
  chmod 700 "$TOR_ROOT" "$TOR_ROOT/run"
  CFG_ARGS+=(--SocksPort "unix:$TOR_SOCKS_SOCKET" --ControlPort 0)
fi

status() {
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    echo "running (pid $(cat "$PIDF"))"
  else
    echo "stopped"
  fi
}

case "${1:-status}" in
  foreground)
    exec "$TOR" "${CFG_ARGS[@]}" --RunAsDaemon 0 ;;
  start)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then echo "already running"; exit 0; fi
    "$TOR" "${CFG_ARGS[@]}"
    for i in $(seq 1 30); do
      grep -q "Bootstrapped 100%" "$TOR_ROOT/run/tor.log" 2>/dev/null && { echo "tor is up and bootstrapped"; exit 0; }
      sleep 1
    done
    echo "tor started (still bootstrapping? check log)"; ;;
  stop)
    [ -f "$PIDF" ] && kill "$(cat "$PIDF")" && rm -f "$PIDF" && echo "stopped" || echo "not running" ;;
  newidentity)
    python3 "$DIR/newnym.py" "$TOR_ROOT/run/control_auth_cookie"
    echo "new identity requested"; ;;
  log) tail -20 "$TOR_ROOT/run/tor.log" ;;
  *) status ;;
esac
