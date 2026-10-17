#!/usr/bin/env python3
"""LOOK AT THE SCREEN. Read-only; nothing here presses a button.

Usage (from tools/, with the stream up):

    ./calibrate.py shot            full-screen capture -> a PNG you can open
    ./calibrate.py verify          sign the crops off against a live game;
                                   writes the stamp grind.py refuses to run
                                   without

Both are safe to run while someone else has the pad.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pad  # noqa: E402
import screen  # noqa: E402

OUT = f"{C.RUN_DIR}/calib"
os.makedirs(OUT, exist_ok=True)


def cmd_shot():
    p = f"{OUT}/screen-{time.strftime('%H%M%S')}.png"
    pad.shot(None, p)
    print(f"  {p}")
    return p


def _stamp_add(key, value):
    d = {}
    try:
        with open(C.CALIB_FILE) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        pass
    d[key] = value
    d["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(C.CALIB_FILE, "w") as fh:
        json.dump(d, fh, indent=1)


def cmd_verify(seconds=120):
    """Sign the crop regions off against a REAL screen, opportunistically.

    A single snapshot cannot do this: each region is only readable in its own
    moment (the tab strip only on a playcall screen; the score only while the
    bar shows the score rather than a stat pop-up; the action bar only before
    a play is called). So watch for up to `seconds`, confirm each region the
    moment it becomes readable, and sign off once every required one has been
    seen. Nothing is pressed, so the game plays on around us.
    """
    seconds = float(seconds)
    checks = {}
    deadline = time.time() + seconds
    print(f"  watching for up to {seconds:.0f}s - nothing will be pressed.")
    print("  each region is confirmed the moment it becomes readable.\n")

    required = ("tabstrip", "clocks", "score")
    while time.time() < deadline:
        if "tabstrip" not in checks:
            side, _i, name, ratio = screen.read_both(
                save=f"{OUT}/verify-tabs.png")
            if side is not None and ratio >= C.CONFIDENT:
                checks["tabstrip"] = {"ok": True, "side": side, "tab": name,
                                      "ratio": round(ratio, 2)}
                print(f"  PASS  TABSTRIP   side={side} tab={name} "
                      f"ratio={ratio:.2f}")

        if "clocks" not in checks:
            q, gs, ps = screen.read_clocks()
            if q is not None and gs is not None:
                checks["clocks"] = {"ok": True, "quarter": q, "game": gs,
                                    "play": ps}
                print(f"  PASS  CLOCKS     Q{q} game={gs}s play={ps}s")

        if "score" not in checks:
            sc = screen.read_score()
            if sc is not None:
                checks["score"] = {"ok": True, "read": sc}
                print(f"  PASS  SCORE      theirs={sc[0]} ours={sc[1]}")

        if "actionbar" not in checks:
            bar = screen.action_bar()
            if any(k in bar for k in ("ADD/REMOVE FAVORITE", "COACH ADJUSTMENTS",
                                      "FLIP PLAY", "PLAYCALL SUBSTITUTIONS")):
                checks["actionbar"] = {"ok": True, "read": bar[:80]}
                print(f"  PASS  ACTIONBAR  {bar[:60]!r}")

        if "badge" not in checks:
            b = screen.read_tier_badge()
            if b:
                checks["badge"] = {"ok": True, "read": b}
                print(f"  PASS  BADGE      {b}")

        if all(k in checks for k in required):
            break
        time.sleep(1.0)

    missing = [k for k in required if k not in checks]
    print()
    if missing:
        print(f"  ⛔ NOT SIGNED OFF - never saw: {', '.join(missing)}")
        print(f"     PNGs are in {OUT}. Open them - a crop that reads nothing is")
        print("     usually looking at the wrong place, not at bad pixels.")
        print("     (score: is your side's label in MUT_EVENT_OUR_CODES? see README)")
        return False
    for opt in ("actionbar", "badge"):
        if opt not in checks:
            print(f"  (optional {opt} never read - not required)")
    _stamp_add("regions", checks)
    print(f"  ✅ signed off -> {C.CALIB_FILE}")
    return True


CMDS = {"shot": cmd_shot, "verify": cmd_verify}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__)
        sys.exit(1)
    fn = CMDS[sys.argv[1]]
    args = sys.argv[2:]
    sys.exit(0 if fn(*args) is not False else 1)
