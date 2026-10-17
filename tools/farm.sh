#!/bin/bash
# Start the farm. APPENDS to a dated log so a restart can never destroy history.
#
#   ./farm.sh          start (then start ./health.sh in the background)
#   ./farm.sh stop     stop cleanly at the next play boundary
cd "$(dirname "$0")" || exit 1

# Resolve the interpreter. Order: an explicit MUT_PY, the repo's venv/ (see
# README), a venv inside tools/, then whatever python3 is on PATH.
resolve_py() {
  # We have already cd'd into the script's directory, so use PWD.
  local here; here="$PWD"
  for c in "$MUT_PY" "$here/../venv/bin/python" "$here/venv/bin/python" \
           "$(command -v python3)"; do
    [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return; }
  done
  echo ""
}
PY="$(resolve_py)"
if [ -z "$PY" ]; then
  echo "No python found. Create the venv (see README)." >&2
  exit 1
fi

# `farm.sh stop` is the clean way to stop: the loop checks for this file at a
# play boundary. A bare pkill can land mid-play, after the snap.
# (If you are taking over the controller yourself, killing it at once is fine.)
mkdir -p /tmp/mut-event
if [ "$1" = "stop" ]; then
  pgrep -f "grind.py loop" >/dev/null || { echo "not running"; exit 0; }
  touch /tmp/mut-event/stop
  echo -n "stopping at the next play boundary"
  for _ in $(seq 1 40); do
    pgrep -f "grind.py loop" >/dev/null || { echo " -> stopped clean"; exit 0; }
    echo -n "."; sleep 1
  done
  # A play cannot outlast 40s. If we are here the loop is wedged, not playing.
  echo " -> no boundary in 40s, forcing"
  rm -f /tmp/mut-event/stop
  pkill -f "grind.py loop"
  exit 0
fi
rm -f /tmp/mut-event/stop

# The reachability check comes AFTER the stop path on purpose, so the farm can
# still be stopped when the console has gone down.
ping -c 1 -t 2 "${MUT_PS5_HOST:-192.168.1.100}" >/dev/null 2>&1 \
  || { echo "PS5 UNREACHABLE - not starting"; exit 1; }

# A HALT (the loop stopped on something it could not handle) is not something a
# restart may step over. Clearing it is a deliberate act.
if [ -f /tmp/mut-event/HALT ]; then
  echo ""
  echo "⛔ FARM HALTED:"
  sed 's/^/   /' /tmp/mut-event/HALT
  echo ""
  echo "   Clear it deliberately to carry on:  rm /tmp/mut-event/HALT"
  echo ""
  exit 1
fi

pgrep -f "grind.py loop" >/dev/null && { echo "already running"; exit 0; }

LOG=/tmp/mut-event/grind-$(date +%Y%m%d).log
{ echo ""; echo "===== farm started $(date '+%Y-%m-%d %H:%M:%S') ====="; } >> "$LOG"
ln -sf "$LOG" /tmp/mut-event/grind.log
nohup "$PY" -u grind.py loop 99999 >> "$LOG" 2>&1 &
sleep 4
if pgrep -f "grind.py loop" >/dev/null; then
  echo "farm RUNNING -> $LOG"
else
  echo "FAILED TO START:"; tail -5 "$LOG"; exit 1
fi
