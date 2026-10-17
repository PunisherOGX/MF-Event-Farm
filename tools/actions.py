#!/usr/bin/env python3
"""Everything that presses a button. Reading lives in screen.py.

Never trust a press count: the playcall tab resets after every play and single
L1/R1 presses get dropped over Remote Play. Press, re-read, confirm.
A log line saying a button was pressed proves it was sent, not received.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config as C  # noqa: E402
import pad  # noqa: E402
import screen  # noqa: E402


# ---------------------------------------------------------------------------
# PLAYCALL NAVIGATION
# ---------------------------------------------------------------------------

def goto_tab(side, target_name, tries=3, known=None):
    """Press L1/R1 until `target_name` is the active tab.

    `known` is the tab index the caller just read; the first pass uses it
    instead of spending another capture.
    """
    names = screen.tab_names(side)
    if target_name not in names:
        return False
    target = names.index(target_name)
    n = len(names)
    for _ in range(tries):
        if known is not None:
            i, ratio, seen = known, 9.9, side
            known = None
        else:
            seen, i, _name, ratio = screen.read_both()
        if seen != side or ratio < C.CONFIDENT:
            # Not on this playcall screen (mid-play, replay, cutscene): wait,
            # never press blind.
            time.sleep(1.5)
            continue
        if i == target:
            return True
        # Shortest wrapped path, sent as one daemon call.
        right = (target - i) % n
        left = (i - target) % n
        btn, count = ("r1", right) if right <= left else ("l1", left)
        pad.press_many([btn] * count, hold=0.06, gap=0.16)
        time.sleep(0.35)
    return False


# ---------------------------------------------------------------------------
# TEMPO: THE CHEW CLOCK
# ---------------------------------------------------------------------------

def set_tempo(want="CHEW", tries=5, log=print):
    """R3 -> step TEMPO to `want` (CHEW / NORMAL), verified by two reads -> back out.

    Offensive playcall screen only. The values are CHEW CLOCK / NORMAL /
    NO HUDDLE and the list wraps, so one LEFT too many lands on NO HUDDLE:
    step one at a time and confirm with two fresh reads.
    """
    pad.press("r3", hold=0.10)
    time.sleep(1.3)
    # Prove the panel opened. If R3 was dropped we are still on the playcall
    # screen, where LEFT presses would scroll the play grid.
    if "TEMPO" not in screen.tempo_label():
        pad.press("moon", hold=0.06)
        time.sleep(0.6)
        return False

    ok = False
    val = ""
    for _ in range(tries):
        val = screen.tempo_value()
        if want in val:
            time.sleep(0.4)
            if want in screen.tempo_value():
                ok = True
                break
        pad.press("left", hold=0.06)
        time.sleep(0.9)
    if not ok:
        log(f"    ! tempo NOT set to {want} (last read: {val!r})")
    pad.press("moon", hold=0.06)
    time.sleep(0.9)
    return ok


# ---------------------------------------------------------------------------
# THE SNAP
# ---------------------------------------------------------------------------
# Returns (snapped, status, wait_hint). snapped=False means no play was spent;
# the caller should wait `wait_hint` seconds and look again.

def _t(t0, what, log):
    """Time every step of the snap path that can block."""
    log(f"      +{time.time() - t0:4.1f}s  {what}")


def _snap_and_run(run_seq, log):
    """Snap, then hold stick UP + R2 from run_seq['after'] for run_seq['hold'].

    The hold is the daemon's /hold_watch: it releases within ~20 ms of the
    playcall screen coming back (play-card band dark AND the L1 badge lit), and
    releases every key in a finally. A blind timed hold would land R2 on the
    playcall screen after a quick tackle, where a bare R2 is FLIP PLAY.
    """
    pad._remote_post("/focus", {})
    time.sleep(0.35)
    t_snap = time.time()
    pad._remote_post("/press", {"buttons": ["cross"], "hold": C.OFF_SNAP_HOLD,
                                "gap": 0.0, "focus": False})
    after, hold = float(run_seq["after"]), float(run_seq["hold"])
    time.sleep(max(0.0, (t_snap + after) - time.time()))
    t_hold = time.time()
    r = None
    try:
        r = pad._remote_post("/hold_watch", {
            "keys": [C.OFF_RUN_STICK, C.OFF_RUN_SPRINT],
            "max_seconds": hold,
            "interval": 0.02,
            "min_hold": 0.15,
            "region": list(C.PLAY_BAND),
            "dark_fraction": C.PLAY_BAND_DARK,
            "confirm_region": list(C.PLAY_L1_BADGE),
            "confirm_bright": C.PLAY_L1_BRIGHT,
            "repress": [], "repress_every": 0.4,
            "focus": False})
    finally:
        try:
            pad.release()
        except Exception:
            pass
    tail = ""
    if isinstance(r, dict):
        if r.get("error"):
            log(f"    ! run hold rejected: {r['error']}")
        elif r.get("reason") == "screen":
            tail = f", released by the screen at {r.get('held')}s"
    return f" + run @{t_hold - t_snap:.2f}s hold {hold:.1f}s{tail}"


def chew_and_hike(run_seq, log=print, allow_skip=True, chew=True):
    """Wait out the play clock while the game clock is running, then snap.

    Every second spent pre-snap burns game clock, but only if the game clock is
    moving. If it is stopped, snap on the floor. If the quarter will end before
    the play clock does, do not snap at all.
    """
    t0 = time.time()

    # Never time a snap against a frozen picture: the play clock would never
    # appear to fall and we would wait into a delay of game.
    if screen.FROZEN_STREAK >= C.STALE_FRAMES:
        log(f"    ! picture frozen ({screen.FROZEN_STREAK} identical frames) "
            f"- NOT pressing until it moves")
        return False, "frozen picture - no play called", 1.5

    def snap(why, urgent=False):
        # `urgent` skips the MIN_SNAP_WAIT floor: the play clock is about to
        # expire, and a delay of game is worse than an early snap.
        if not urgent:
            time.sleep(max(0.0, (t0 + C.MIN_SNAP_WAIT) - time.time()))
        _t(t0, "SNAPPING", log)
        return True, why + _snap_and_run(run_seq, log), 0.0

    if C.HIKE_AT >= 99 or not chew:
        # Even without chewing, look at the play clock once: it can already be
        # low on arrival, and the floor would then run it out.
        _q, _gs, ps0 = screen.read_clocks()
        if ps0 is not None and ps0 <= C.MIN_SNAP_WAIT + 3:
            return snap(f"snapped NOW (play clock :{ps0} - no time for the floor)",
                        urgent=True)
        return snap(f"snapped at +{C.MIN_SNAP_WAIT:.1f}s (chew OFF)")

    _t(t0, "game clock read 1", log)
    _q0, ga, ps_now = screen.read_clocks()

    if ps_now is not None and ps_now <= C.HIKE_AT:
        _t(t0, f"play clock {ps_now} on arrival - snapping", log)
        return snap(f"snapped at play clock :{ps_now} (already low on arrival)",
                    urgent=True)

    if ps_now is not None and ps_now <= C.CHEW_SKIP_BELOW:
        time.sleep(C.MIN_SNAP_WAIT)
        _t(t0, f"arrived at :{ps_now} - nothing left to chew, snapping", log)
        return snap(f"snapped at +{C.MIN_SNAP_WAIT:.1f}s "
                    f"(arrived at :{ps_now})", urgent=True)

    # Unread on arrival: not knowing is not the same as having time.
    if ps_now is None:
        time.sleep(C.MIN_SNAP_WAIT)
        _t(t0, "play clock unread on arrival - snapping on the floor", log)
        return snap("snapped on the floor (play clock unread on arrival)",
                    urgent=True)

    # The floor doubles as the stopped-clock test: read the game clock again.
    time.sleep(C.MIN_SNAP_WAIT)
    _t(t0, "game clock read 2", log)
    _q1, gb, _ps = screen.read_clocks()

    if ga is None or gb is None:
        _t(t0, "clock unreadable after the floor - snapping", log)
        return snap("snapped on the floor (clock unreadable)", urgent=True)

    if gb >= ga:
        if screen.FROZEN_STREAK >= C.STALE_FRAMES:
            log(f"    clock looks stopped but the PICTURE is frozen "
                f"({screen.FROZEN_STREAK} identical frames) - not acting")
            return False, "frozen picture - no play called", 1.5
        _t(t0, "game clock stopped - snapping now", log)
        return snap(f"snapped now (game clock stopped at {gb}s - nothing to "
                    f"burn)", urgent=True)

    deadline = t0 + C.SNAP_DEADLINE
    misses = 0
    while time.time() < deadline:
        _q, gs, ps = screen.read_clocks()
        if ps is None:
            misses += 1
            if misses >= 4:
                break
            time.sleep(0.8)
            continue
        misses = 0

        # The quarter runs out first: let it, with no play spent. +3 covers
        # read latency.
        if allow_skip and gs is not None and gs + 3 < ps:
            return (False,
                    f"quarter ends first (game {gs}s < play {ps}s) - NOT snapping",
                    min(float(gs) + 3.0, 25.0))

        if ps <= C.HIKE_AT:
            return snap(f"snapped at play clock :{ps}", urgent=True)

        # Schedule the snap from one reading rather than polling towards it:
        # each read costs a second or two, so polling overshoots the target.
        wait = ps - C.HIKE_AT
        if wait <= C.SNAP_SCHEDULE_MAX:
            time.sleep(max(0.0, wait))
            return snap(f"snapped at play clock :~{C.HIKE_AT} "
                        f"(scheduled from :{ps})", urgent=True)
        time.sleep(max(0.6, min(4.0, wait - C.SNAP_SCHEDULE_MAX)))
    return snap("snapped (fallback - clock unreadable)")


# ---------------------------------------------------------------------------
# CALLING THE PLAYS
# ---------------------------------------------------------------------------

def _play_row_ok(play, log=print):
    """True if `play` is on the visible favourites row, or the row is
    unreadable. Scrolls with the d-pad to find it when another row shows.

    False only when the names are readable and ours is not among them after
    PLAY_ROW_SCROLL_TRIES scrolls; the caller then does not press.
    """
    if not C.PLAY_ROW_CHECK:
        return True
    import ocr
    # First two words, spaces stripped: Vision often drops the space.
    want = " ".join(play["name"].upper().split()[0:2])
    scroll = ["up", "down", "down", "up"]            # net zero if all fail
    for attempt in range(C.PLAY_ROW_SCROLL_TRIES + 1):
        try:
            row = str(ocr.ocr(C.PLAY_ROW, path=f"{C.SHOT_DIR}/playrow.png")).upper()
        except Exception:
            row = ""
        if want in row or want.replace(" ", "") in row.replace(" ", ""):
            if attempt:
                log(f"    play row fixed after {attempt} scroll(s)")
            return True
        if len(row.replace("|", "").strip()) < 8:
            log(f"    play row unreadable ({row[:30]!r}) - pressing unverified")
            return True
        if attempt >= C.PLAY_ROW_SCROLL_TRIES:
            break
        key = scroll[attempt % len(scroll)]
        log(f"    wrong favourites row ({row[:60]!r}) - {want} not visible, "
            f"scrolling {key}")
        pad.press(key, hold=0.10)
        time.sleep(0.45)
    log(f"    {want} NOT FOUND on any row - NOT pressing {play['button']}")
    return False


_OVERLAY_CALLS = 0


def _clear_held_overlay(log=print):
    """If Madden thinks a play button is being HELD, tap it to clear that.

    A lost key-up can latch the playcall screen into "[RELEASE] SELECT ...",
    after which every cross reads as still holding. Only a press-and-release of
    the same button clears it. Checked on every OVERLAY_CHECK_EVERY-th call.
    """
    global _OVERLAY_CALLS
    _OVERLAY_CALLS += 1
    if _OVERLAY_CALLS % max(1, C.OVERLAY_CHECK_EVERY):
        return
    try:
        bar = screen.action_bar()
    except Exception:
        return
    if "RELEASE" not in bar:
        return
    log(f"    ! held-button overlay on the playcall screen ({bar[:40]!r}) "
        f"- tapping {C.DEF_PLAY_BUTTON} to clear it")
    # Never tap cross here: cross is the snap button, and on the offensive
    # playcall screen it would pick whatever play sits on X.
    for key in dict.fromkeys(k for k in ("box", C.DEF_PLAY_BUTTON) if k != "cross"):
        pad.press(key, hold=0.12)
        time.sleep(0.8)
        try:
            bar = screen.action_bar()
        except Exception:
            bar = ""
        if "RELEASE" not in bar:
            log(f"    overlay cleared by tapping {key}")
            return
    log(f"    ! overlay still present after taps: {bar[:40]!r}")


def _ready_to_press(log):
    """Common start of every play call: refuse a frozen picture, lift any
    stuck key, clear a latched overlay. Returns an early result or None."""
    if screen.FROZEN_STREAK >= C.STALE_FRAMES:
        log(f"    ! picture frozen ({screen.FROZEN_STREAK} identical frames) "
            f"- NOT pressing until it moves")
        return False, "frozen picture - no play called", 1.5
    # A key-down whose key-up was lost (e.g. across a stream drop) leaves chiaki
    # believing it is still held. Releasing costs nothing when nothing is held.
    try:
        pad.release()
    except Exception:
        pass
    _clear_held_overlay(log)
    return None


def call_offense(play, known=None, log=print, allow_skip=True, chew=True):
    """FAVORITES -> check the row -> play button -> chew -> snap -> run."""
    early = _ready_to_press(log)
    if early:
        return early
    if not goto_tab("offense", C.OFF_TAB, known=known):
        return False, f"could not reach {C.OFF_TAB}", 0.0
    if not _play_row_ok(play, log):
        return False, "wrong favourites row - play not called", 1.0
    # Confirm the tab right before pressing: a cutscene can drop us back on
    # COACH SUGGESTIONS, and the press would call whatever sits there.
    try:
        _side, _i, _name, _ratio = screen.read_both()
        if _side == "offense" and _ratio >= C.CONFIDENT and _name != C.OFF_TAB:
            log(f"    tab drifted to {_name} before the press - "
                f"re-navigating to {C.OFF_TAB}")
            if not goto_tab("offense", C.OFF_TAB):
                return False, f"could not re-reach {C.OFF_TAB}", 1.0
    except Exception:
        pass
    pad.press(play["button"], hold=C.OFF_SELECT_HOLD)
    snapped, status, hint = chew_and_hike(play["run_seq"], log=log,
                                          allow_skip=allow_skip, chew=chew)
    return snapped, f"{play['name']} - {status}", hint


def call_defense(known=None, log=print):
    """FAVORITES -> the defensive play (a plain press). Nothing to snap."""
    early = _ready_to_press(log)
    if early:
        return early
    if not goto_tab("defense", C.DEF_TAB, known=known):
        return False, f"could not reach {C.DEF_TAB}", 0.0
    pad.press(C.DEF_PLAY_BUTTON, hold=C.OFF_SELECT_HOLD)
    return True, f"{C.DEF_PLAY_NAME} called on {C.DEF_TAB}", 0.0


def call_special_teams(log=print):
    """Punt and kickoff: take the middle play, then snap."""
    t = time.time()
    pad.press("cross", hold=0.06)
    time.sleep(max(0.0, (t + C.MIN_SNAP_WAIT) - time.time()))
    pad.press("cross", hold=0.06)
    return True, "special teams: selected + snapped", 0.0


# ---------------------------------------------------------------------------
# VERIFIED MENU SELECTION (end-of-game menu, popups)
# ---------------------------------------------------------------------------
# Find each option by its words wherever it is, measure which row is actually
# highlighted, step toward the one we want and re-verify. A layout change makes
# this slower, never wrong.

def _rows_from(path, spec):
    """One capture -> [{key, text, cy, bright}] for the options in `spec`.
    Text and brightness come from the same frame."""
    boxes = screen.ocr_boxes_file(path)
    buf, w, h, bpr, bpp = screen.load(path)
    found = []
    for b in boxes:
        up = b["text"].upper()
        for key, rule in spec.items():
            if any(bad in up for bad in rule["not"]):
                continue
            if not any(good in up for good in rule["want"]):
                continue
            # Sample the button around the text, not the whole row.
            half = max(12.0, (b["y1"] - b["y0"]) * 0.9)
            padx = max(20.0, (b["x1"] - b["x0"]) * 0.12)
            found.append({
                "key": key,
                "text": b["text"],
                "cy": b["cy"],
                "bright": screen.box_mean(buf, w, h, bpr, bpp,
                                          b["x0"] - padx, b["x1"] + padx,
                                          b["cy"] - half, b["cy"] + half),
            })
            break
    # Every hit is kept: a heading repeating the words is never highlighted,
    # so it can never win the brightness test.
    return sorted(found, key=lambda r: r["cy"])


def _highlighted(rows, margin=1.18):
    """The focused row, or None if it is not clearly brighter than the rest."""
    if len(rows) < 2:
        return None
    ordered = sorted(rows, key=lambda r: r["bright"], reverse=True)
    top, second = ordered[0], ordered[1]
    if second["bright"] <= 0 or top["bright"] / second["bright"] < margin:
        return None
    return top


def select_row(spec, want, label="menu", log=print, tries=6, shot=None):
    """Put focus on the row matching `want`, confirm it twice, press cross.

    Returns (ok, detail). If focus can never be read confidently it presses
    nothing, so the caller can alert instead of committing to a guess.
    """
    shot = shot or f"{C.SHOT_DIR}/_select.png"
    unknown = 0
    for _attempt in range(tries):
        pad.shot(None, shot)
        rows = _rows_from(shot, spec)
        if len({r["key"] for r in rows}) < 2:
            names = ", ".join(sorted({r["key"] for r in rows})) or "none"
            log(f"    {label}: only found [{names}] - looking again")
            time.sleep(1.0)
            unknown += 1
            if unknown >= 3:
                return False, f"could not find both options (saw {names})"
            continue

        focus = _highlighted(rows)
        detail = "  ".join(f"{r['key']}={r['bright']:.0f}" for r in rows)
        if focus is None:
            unknown += 1
            log(f"    {label}: focus not decisive ({detail}) - looking again")
            if unknown >= 3:
                return False, f"focus never read confidently ({detail})"
            time.sleep(1.0)
            continue

        if focus["key"] == want:
            time.sleep(0.5)
            pad.shot(None, shot)
            again = _highlighted(_rows_from(shot, spec))
            if again is not None and again["key"] == want:
                log(f"    {label}: {want} focused and confirmed twice "
                    f"({detail}) -> cross")
                pad.press("cross", hold=0.06)
                time.sleep(3.0)
                return True, f"selected {want}"
            log(f"    {label}: second read disagreed - looking again")
            continue

        # Step toward the nearest matching row.
        cands = [r for r in rows if r["key"] == want]
        if not cands:
            return False, f"{want} not on screen"
        want_row = min(cands, key=lambda r: abs(r["cy"] - focus["cy"]))
        btn = "down" if want_row["cy"] > focus["cy"] else "up"
        log(f"    {label}: focus on {focus['key']}, {want} is {btn} "
            f"({detail}) -> {btn}")
        pad.press(btn, hold=0.06)
        time.sleep(0.8)
    return False, f"gave up after {tries} attempts"


def choose_finish_game(log=print, tries=6, shot=None):
    """Select FINISH GAME on Madden's end-of-game menu."""
    return select_row(C.POSTGAME_ROWS, C.POSTGAME_WANT, "end of game",
                      log=log, tries=tries, shot=shot)
