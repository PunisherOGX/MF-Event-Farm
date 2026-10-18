#!/bin/bash
# The single source of truth. Reads the PERSISTENT history, never the volatile log.
cd "$(dirname "$0")" || exit 1

# Resolve the interpreter. Order: an explicit MUT_PY, this repo's venv, then a
# sibling qenv, then whatever python3 is on PATH.
resolve_py() {
  # ⛔ We have already cd'd into the script's directory, so use PWD.
  # "$(dirname "$0")" here would resolve against the NEW cwd and point at a
  # directory that does not exist.
  local here; here="$PWD"
  for c in "$MUT_PY" "$here/../venv/bin/python" "$here/qenv/bin/python" \
           "$(command -v python3)"; do
    [ -n "$c" ] && [ -x "$c" ] && { echo "$c"; return; }
  done
  echo ""
}
PY="$(resolve_py)"
if [ -z "$PY" ]; then
  echo "No python found. Run ./setup.sh first." >&2
  exit 1
fi

echo "=== FARM ==="
pgrep -f "grind.py loop" >/dev/null && echo "  RUNNING" || echo "  STOPPED"
L=$(ls -t /tmp/mut-event/grind-*.log 2>/dev/null | head -1)
[ -n "$L" ] && echo "  log: $L  (written $(( $(date +%s) - $(stat -f %m "$L") ))s ago)"
echo "  state: $(cat ~/.mut_event_state.json 2>/dev/null || echo none)"
if [ -f ~/.mut_event_calibration.json ]; then
  echo "  calib: $(tr -d '\n' < ~/.mut_event_calibration.json | cut -c1-90)"
else
  echo "  calib: ⛔ NOT VERIFIED - grind.py will refuse to start"
fi
echo
echo "=== LAST 10 COMPLETED GAMES (persistent history) ==="
echo "  time          badge   layout   score  plays  mins   gap"
"$PY" - <<'PY'
import json, os
# Must match config.HISTORY.
F = os.path.expanduser(os.environ.get("MUT_EVENT_HISTORY",
                                      "~/.mut_most_feared_history.jsonl"))
if not os.path.exists(F):
    print("  no games recorded yet"); raise SystemExit
rows = [json.loads(l) for l in open(F) if l.strip()]
rows = [r for r in rows if (r.get("plays") or 0) > 0]
if not rows:
    print("  no games recorded yet"); raise SystemExit
for r in rows[-10:]:
    conf = "" if r.get("opp_confirmed") is not False else "  (opp score assumed)"
    ot = "  OT" if r.get("overtime") else ""
    if r.get("partial"): ot += "  PARTIAL(joined Q%s)" % r.get("first_quarter", "?")
    print(f"  {r.get('ts','')[5:16]}  #{str(r.get('event') or '?'):<5}"
          f"{str(r.get('badge') or '?'):<7} "
          f"{str(r.get('hud_layout') or '?'):<8} "
          f"{r.get('theirs')}-{r.get('ours')} {r.get('result')}  "
          f"{r['plays']:>3}p {r['min']:>5}m  gap {r.get('gap',0):>3}s{ot}{conf}")
w = sum(1 for r in rows if r.get("result") == "W")
l_ = sum(1 for r in rows if r.get("result") == "L")
t = sum(1 for r in rows if r.get("result") == "T")
# ⛔ Pace stats use FULL games only. A row from a mid-game restart is a real
# result but a meaningless duration.
full = [r for r in rows if not r.get("partial")]
mins = [r["min"] for r in full if r.get("min")]
plays = [r["plays"] for r in full if r.get("plays")]
print(f"\n  tracked: {w}W-{l_}L" + (f"-{t}T" if t else "")
      + (f"   median {sorted(mins)[len(mins)//2]:.1f} min / "
         f"{sorted(plays)[len(plays)//2]} plays" if mins else ""))
if mins:
    hrs = sum(mins) / 60 + sum(r.get("gap", 0) for r in full) / 3600
    print(f"  throughput: {len(full)/hrs:.1f} games/hour  (from {len(full)} full games)"
          if hrs else "")
else:
    print("  no FULL games yet - pace unknown (partial rows are excluded)")
PY
echo
echo "=== MOST FEARED: KEYS AND RUNS ==="
"$PY" "$(dirname "$0")/keys.py"
echo "  --- last 8 run events ---"
tail -8 ~/.mut_most_feared_runs.jsonl 2>/dev/null | sed 's/^/  /' || echo "  none yet"
echo
echo "=== NEEDS ATTENTION ==="
[ -f /tmp/mut-event/HALT ] && { echo "  ⛔ HALTED:"; cat /tmp/mut-event/HALT; }
[ -f /tmp/mut-event/FROZEN ] && { echo "  🧊 FROZEN:"; cat /tmp/mut-event/FROZEN; }
echo "  --- sticky alerts (only a human clears these) ---"
tail -5 /tmp/mut-event/ALERT.STICKY 2>/dev/null || echo "  none"
