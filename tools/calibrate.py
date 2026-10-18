#!/usr/bin/env python3
"""LOOK AT THE SCREEN. The tool that makes that the cheap option.

⭐ THE ONE HABIT THAT MATTERS MOST on this project: capture a frame and read it,
never reason about what the code probably does. Nearly every long debugging
session here came from describing behaviour instead of looking, and each was
then solved in seconds by looking. This exists so "just look" is one command.

Usage (from new-event/tools, with the stream up):

    ./calibrate.py shot            full-screen capture -> a PNG you can open
    ./calibrate.py text            full-screen OCR, printed
    ./calibrate.py regions         every configured crop: PNG + what it reads
    ./calibrate.py tabs            live tab-strip read, repeated
    ./calibrate.py clocks          live quarter / game clock / play clock
    ./calibrate.py score           live score read, both boxes
    ./calibrate.py badge           the ARCADE / COMP live-play badge
    ./calibrate.py event           the events list, for EVENT_MATCH
    ./calibrate.py entry           the ENTRY OPTIONS screen + focus analysis
    ./calibrate.py verify          sign the crops off; writes the stamp
                                   grind.py refuses to run without

    ./calibrate.py tempo           ⚠️ PRESSES R3 - opens COACH ADJUSTMENTS
    ./calibrate.py snapfloor N     ⚠️ PRESSES CROSS - tests MIN_SNAP_WAIT

⛔ The two marked commands send input. Everything else is READ-ONLY and is safe
to run while the player has the pad.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pad  # noqa: E402
import screen  # noqa: E402
import ocr  # noqa: E402

OUT = f"{C.RUN_DIR}/calib"
os.makedirs(OUT, exist_ok=True)

REGIONS = [
    ("TABSTRIP", C.TABSTRIP, "the playcall tab strip"),
    ("CLOCKS", C.CLOCKS, "quarter | game clock | play clock | down | spot"),
    ("SCORE_L", C.SCORE_L, "opponent CODE + score"),
    ("SCORE_R", C.SCORE_R, "our score + CODE"),
    ("ACTIONBAR", C.ACTIONBAR, "ADD/REMOVE FAVORITE ... BACK"),
    ("TEMPO_LABEL", C.TEMPO_LABEL, "the words TEMPO ADJUSTMENT (panel only)"),
    ("TEMPO_VALUE", C.TEMPO_VALUE, "the < NORMAL > box (panel only)"),
]


def cmd_shot():
    p = f"{OUT}/screen-{time.strftime('%H%M%S')}.png"
    pad.shot(None, p)
    print(f"  {p}")
    return p


def cmd_text():
    t = screen.screen_text()
    print(f"  {len(t)} chars\n")
    print(t)


def cmd_regions():
    print("  region        reads as")
    print("  " + "-" * 70)
    for name, region, what in REGIONS:
        path = f"{OUT}/{name}.png"
        try:
            txt = str(ocr.ocr(region, path=path))
        except Exception as e:
            txt = f"<FAILED: {e}>"
        print(f"  {name:<13s} {txt[:56]!r}")
        print(f"  {'':<13s}   {what}  {region}  -> {path}")
    print("\n  ⛔ Open the PNGs. A region that OCRs to something plausible can "
          "still\n     be cropping the wrong thing - only the picture proves it.")


def cmd_tabs(n=6):
    for k in range(n):
        side, i, name, ratio = screen.read_both(save=f"{OUT}/tabs-{k}.png")
        flag = "OK " if (side and ratio >= C.CONFIDENT) else "   "
        print(f"  {flag}side={str(side):<13s} tab={name:<14s} ratio={ratio:.2f}"
              f"  frozen_streak={screen.FROZEN_STREAK}")
        time.sleep(1.0)


def cmd_clocks(n=6):
    print("  quarter  game  play")
    for _ in range(n):
        q, gs, ps = screen.read_clocks()
        g = f"{gs//60}:{gs%60:02d}" if gs is not None else "----"
        print(f"  Q{q if q else '?'}       {g:>5s} {ps if ps is not None else '--':>5}")
        time.sleep(1.5)


def cmd_score(n=4):
    for _ in range(n):
        sc = screen.read_score()
        print(f"  {sc}")
        time.sleep(1.2)


def cmd_badge(n=8):
    print("  ⭐ Only renders during a LIVE PLAY. None is normal between snaps.")
    for _ in range(n):
        print(f"  {screen.read_tier_badge()}")
        time.sleep(1.5)


def cmd_event():
    """Dump the events list so EVENT_MATCH can be written from the real title."""
    p = cmd_shot()
    t = screen.screen_text()
    print(f"\n  full text ({len(t)} chars):\n")
    print(t)
    print(f"\n  screenshot: {p}")
    print("\n  ⛔ Pick a substring that is UNIQUE to our card and set it as "
          "EVENT_MATCH\n     in config.py. It is a REGEX, matched "
          "case-insensitively.\n"
          "     Do not match on punctuation - Vision renders '(CPU)' as "
          "'(CPU.' about\n     half the time.")


def cmd_entry():
    """Dump the ENTRY OPTIONS screen and show exactly how focus reads.

    ⛔ This is the screen that cost ~11 tier rewards last event, because the
    code assumed which row had focus. Look at the numbers below BEFORE trusting
    `choose_lineup` unattended: the focused row should be clearly brighter.
    """
    import actions
    p = f"{OUT}/entry-{time.strftime('%H%M%S')}.png"
    pad.shot(None, p)
    rows = actions._rows_from(p, C.LINEUP_ROWS)
    print(f"  screenshot: {p}\n")
    if not rows:
        print("  no lineup options matched. Full text follows - adjust "
              "config.LINEUP_ROWS.\n")
        print(screen.screen_text())
        return
    print("  key           brightness  y      text")
    print("  " + "-" * 62)
    for r in rows:
        print(f"  {r['key']:<13s} {r['bright']:>9.1f}  {r['cy']:>5.0f}  "
              f"{r['text'][:32]!r}")
    focus = actions._highlighted(rows)
    print()
    if focus is None:
        print("  ⛔ FOCUS NOT DECISIVE. The rows are too close in brightness "
              "for the\n     1.18 margin. Either the highlight is not a light "
              "box on this screen,\n     or the band is sampling the wrong "
              "rows. LOOK AT THE PNG.")
    else:
        print(f"  focus reads as: {focus['key']}"
              + ("   ✅ this is what we want"
                 if focus["key"] == C.LINEUP_WANT else
                 f"   -> would step toward {C.LINEUP_WANT}"))


def cmd_tempo():
    """⚠️ PRESSES R3. Only run this on an OFFENSIVE playcall screen."""
    print("  pressing R3 (COACH ADJUSTMENTS)...")
    pad.press("r3", hold=0.10)
    time.sleep(1.3)
    label = screen.tempo_label()
    value = screen.tempo_value()
    for name, region in (("TEMPO_LABEL", C.TEMPO_LABEL),
                         ("TEMPO_VALUE", C.TEMPO_VALUE)):
        pad.shot(region, f"{OUT}/{name}-open.png")
    print(f"  TEMPO_LABEL reads {label!r}   (want 'TEMPO')")
    print(f"  TEMPO_VALUE reads {value!r}   (want NORMAL / CHEW CLOCK / NO HUDDLE)")
    print("  backing out with circle")
    pad.press("moon", hold=0.06)
    if "TEMPO" in label:
        _stamp_add("tempo", {"label": label, "value": value})
        print("  ✅ tempo regions verified and recorded")
    else:
        print("  ⛔ TEMPO_LABEL did not read 'TEMPO'. Either R3 did not "
              "register or the\n     crop is wrong. Open the PNGs in " + OUT)


def cmd_snapfloor(wait):
    """⚠️ PRESSES CROSS. Test whether the game accepts a snap sooner.

    ⭐ THE SINGLE BIGGEST UNTESTED LEVER carried over from the old build: it
    waited 6.0s before every snap on an unverified comment - about 4 min/game -
    and nobody ever checked.

    Stand on an OFFENSIVE playcall screen with the play already selected, run
    this, and WATCH THE SCREEN: did the ball actually snap?
    """
    wait = float(wait)
    print(f"  waiting {wait}s then pressing CROSS. Watch whether it snaps.")
    t = time.time()
    time.sleep(wait)
    pad.press("cross", hold=0.06)
    print(f"  pressed at +{time.time()-t:.2f}s")
    time.sleep(2.5)
    side, _i, name, ratio = screen.read_both()
    if side is None or ratio < C.CONFIDENT:
        print("  -> playcall screen is GONE: the snap was accepted.")
    else:
        print(f"  -> still on {side}/{name}: the snap was NOT accepted at "
              f"{wait}s.")


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
    """Sign the crop regions off against a REAL screen, OPPORTUNISTICALLY.

    ⛔ A SINGLE SNAPSHOT CANNOT DO THIS, and trying cost a play the first time.
    Each region is only readable in its own moment:

      * TABSTRIP  only on a playcall screen - blank pre-snap and mid-play
      * SCORE     only when the bar is showing the SCORE. It cycles stat
                  pop-ups ("KAM CHANCELLOR | 1 TACKLE") through the very same
                  pixels, and the team-code guard correctly rejects those
      * ACTIONBAR reads "• PREPLAY" once the play is called, not the playcall
                  action bar

    So: watch for up to `seconds`, confirm each region the moment it becomes
    readable, and sign off once every required one has been seen at least once.
    Nothing is pressed, so the game plays on around us.
    """
    seconds = float(seconds)
    checks = {}
    deadline = time.time() + seconds
    print(f"  watching for up to {seconds:.0f}s - nothing will be pressed.")
    print("  each region is confirmed the moment it becomes readable.\n")

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
                print(f"  PASS  SCORE      theirs={sc[0]} ours={sc[1]}"
                      + ("  [opp zero inferred]" if sc[2] else ""))

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

        required = ("tabstrip", "clocks", "score")
        if all(k in checks for k in required):
            break
        time.sleep(1.0)

    required = ("tabstrip", "clocks", "score")
    missing = [k for k in required if k not in checks]
    print()
    if missing:
        print(f"  ⛔ NOT SIGNED OFF - never saw: {', '.join(missing)}")
        print(f"     PNGs are in {OUT}. Open them - a crop that reads nothing is")
        print("     usually looking at the wrong place, not at bad pixels.")
        return False
    for opt in ("actionbar", "badge"):
        if opt not in checks:
            print(f"  (optional {opt} never read - not required)")
    _stamp_add("regions", checks)
    _stamp_add("event_match", C.EVENT_MATCH or None)
    print(f"  ✅ signed off -> {C.CALIB_FILE}")
    return True


def cmd_unstick():
    """The playcall page is stuck on "[RELEASE] SELECT STUNT" - find what clears it.

    This state ate 60 minutes and was written up as "the Madden
    freeze, quit by hand". The player then cleared it by mashing his controller,
    so it IS recoverable - we just do not know by which button. This sends
    every mapped key ONE AT A TIME, reads the action bar after each, and stops
    at the first one that clears it. Run it ON the stuck playcall page.
    ⛔ Stop the farm first (./farm.sh stop) - two pads fighting proves nothing.
    """
    order = ["lstick_down", "lstick_up", "lstick_left", "lstick_right",
             "rstick_down", "rstick_up", "l3", "r3", "l1", "r1", "l2", "r2",
             "down", "up", "left", "right", "box", "pyramid", "moon", "cross",
             "options"]
    bar = screen.action_bar()
    print(f"  bar now: {bar!r}")
    if "RELEASE" not in bar:
        print("  not stuck (no [RELEASE] on the bar) - nothing to do")
        return True
    for key in order:
        pad.press(key, hold=0.15)
        time.sleep(1.2)
        t = (screen.screen_text() or "").upper()
        on_playcall = "SELECT A PLAY" in t or "FAVORITES" in t
        bar = screen.action_bar()
        print(f"  {key:<13s} -> playcall={on_playcall!s:<5s} bar={bar[:34]!r}")
        if on_playcall and "RELEASE" not in bar:
            print(f"\n  ✅ CLEARED BY {key} - write this in the handoff")
            return True
        if not on_playcall:
            print("  (left the playcall page - waiting for it to come back)")
            for _ in range(15):
                time.sleep(1.0)
                t = (screen.screen_text() or "").upper()
                if "SELECT A PLAY" in t or "FAVORITES" in t:
                    break
            bar = screen.action_bar()
            if "RELEASE" not in bar:
                print(f"\n  ✅ CLEARED around {key} (bar {bar[:30]!r})")
                return True
    print("\n  ⛔ nothing the daemon sends clears it - use the physical pad, one "
          "button at a time, and note which one works")
    return False


CMDS = {
    "shot": cmd_shot, "text": cmd_text, "regions": cmd_regions,
    "tabs": cmd_tabs, "clocks": cmd_clocks, "score": cmd_score,
    "badge": cmd_badge, "event": cmd_event, "entry": cmd_entry,
    "tempo": cmd_tempo, "verify": cmd_verify, "snapfloor": cmd_snapfloor,
    "unstick": cmd_unstick,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print(__doc__)
        sys.exit(1)
    fn = CMDS[sys.argv[1]]
    args = sys.argv[2:]
    sys.exit(0 if fn(*args) is not False else 1)
