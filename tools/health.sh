#!/bin/bash
# THE WATCHER. Blocks until something needs a human, prints why, and exits.
#
# Run it in the background right after ./farm.sh, from whatever is supervising
# the farm (a terminal, or an AI agent's background-task tool). Its exit IS the
# notification, so launch it in a way that tells you when it exits: a plain
# `nohup ... &` does not.
#
# It trips when:
#   - the grind.py process is gone
#   - a HALT or FROZEN file appears, or a new line lands in ALERT.STICKY
#   - the log stops growing for MUT_EVENT_STALE seconds (default 420)
#   - no game has finished for MUT_EVENT_NO_GAME seconds (default 2700): the
#     loop is busy but stuck, e.g. backing out of the same screen forever
D=/tmp/mut-event
LOG=$D/grind.log
sticky_count() { [ -f "$D/ALERT.STICKY" ] && wc -l < "$D/ALERT.STICKY" || echo 0; }
# grep -c prints 0 AND exits 1 when nothing matches, so "|| echo 0" produced
# "0<newline>0" on a fresh log and broke every comparison.
games_done() { local n; n=$(grep -c "GAME [0-9]* END" "$LOG" 2>/dev/null); echo "${n:-0}"; }
STICKY_BEFORE=$(sticky_count)
STALE_SECS=${MUT_EVENT_STALE:-420}
NO_GAME_SECS=${MUT_EVENT_NO_GAME:-2700}

# Only trip on files written AFTER this watcher started: a leftover file from
# an earlier run is not an event.
ARMED=$(date +%s)
newer_than_arm() {
  [ -f "$1" ] || return 1
  [ "$(stat -f %m "$1")" -ge "$ARMED" ]
}
LAST_GAMES=$(games_done)
LAST_GAME_T=$ARMED

while true; do
  sleep 30

  if ! pgrep -f "grind.py loop" >/dev/null; then
    echo "FARM STOPPED - the grind.py process is gone."
    tail -15 "$LOG" 2>/dev/null
    exit 0
  fi

  if newer_than_arm "$D/HALT"; then
    echo "FARM HALTED - it stopped BEFORE entering another game:"
    cat "$D/HALT"
    echo "To carry on: rm $D/HALT && ./farm.sh"
    exit 0
  fi

  if newer_than_arm "$D/FROZEN"; then
    echo "GAME FROZEN - the loop aborted. Madden has to be quit by hand."
    cat "$D/FROZEN"
    exit 0
  fi

  NOW=$(sticky_count)
  if [ "$NOW" -gt "$STICKY_BEFORE" ]; then
    echo "NEW STICKY ALERT:"
    tail -n $(( NOW - STICKY_BEFORE )) "$D/ALERT.STICKY"
    exit 0
  fi

  if [ -f "$LOG" ]; then
    AGE=$(( $(date +%s) - $(stat -L -f %m "$LOG") ))
    if [ "$AGE" -gt "$STALE_SECS" ]; then
      echo "LOG STALE - nothing written for ${AGE}s. The loop is alive but not progressing."
      tail -15 "$LOG"
      exit 0
    fi
  fi

  G=$(games_done)
  if [ "$G" -gt "$LAST_GAMES" ]; then
    LAST_GAMES=$G; LAST_GAME_T=$(date +%s)
  elif [ $(( $(date +%s) - LAST_GAME_T )) -gt "$NO_GAME_SECS" ]; then
    echo "NO GAME FINISHED in $(( NO_GAME_SECS / 60 )) min (games logged: $G)."
    grep -v "\.\.\. waiting" "$LOG" | tail -12
    exit 0
  fi
done
