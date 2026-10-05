#!/usr/bin/env bash
# torctl.sh — manage the local Tor daemon in ./run (relative to this file)
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
TOR="$DIR/bin/tor"
PIDF="$DIR/run/tor.pid"
export LD_LIBRARY_PATH="$DIR/lib"
mkdir -p "$DIR/run"

# Absolute runtime paths passed on the command line (RunAsDaemon needs absolute).
CFG_ARGS=(
  -f "$DIR/torrc"
  --DataDirectory "$DIR/run"
  --PidFile "$PIDF"
  --Log "notice file $DIR/run/tor.log"
)

status() {
  if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then
    echo "running (pid $(cat "$PIDF"))"
  else
    echo "stopped"
  fi
}

case "${1:-status}" in
  start)
    if [ -f "$PIDF" ] && kill -0 "$(cat "$PIDF")" 2>/dev/null; then echo "already running"; exit 0; fi
    "$TOR" "${CFG_ARGS[@]}"
    for i in $(seq 1 30); do
      grep -q "Bootstrapped 100%" "$DIR/run/tor.log" 2>/dev/null && { echo "tor is up and bootstrapped"; exit 0; }
      sleep 1
    done
    echo "tor started (still bootstrapping? check log)"; ;;
  stop)
    [ -f "$PIDF" ] && kill "$(cat "$PIDF")" && rm -f "$PIDF" && echo "stopped" || echo "not running" ;;
  newidentity)
    python3 "$DIR/newnym.py" "$DIR/run/control_auth_cookie"
    echo "new identity requested"; ;;
  log) tail -20 "$DIR/run/tor.log" ;;
  *) status ;;
esac
