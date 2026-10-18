#!/usr/bin/env python3
"""The event farm loop.

    read the screen -> work out the situation -> act -> repeat

Nothing here is time-based. Play length, penalties, turnovers and possession
changes are all handled by re-reading the screen on every pass. When a read is
not confident, the loop waits: sitting still costs at most a delay of game,
pressing blind can call the wrong play.

Event settings live in config.py, reading in screen.py / hud.py, pressing in
actions.py. This file is the loop and the between-game menus.

    ./grind.py loop [max_plays]   the farm (use farm.sh, which logs it)
    ./grind.py menus|event|lineup|chew|off|def   one step, for testing
"""
import atexit
import functools
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import actions  # noqa: E402
import config as C  # noqa: E402
import hud  # noqa: E402
import keys  # noqa: E402
import pad  # noqa: E402
import screen  # noqa: E402

STOP_FILE = "/tmp/mut-event/stop"
PAUSE_NEW_RUN = "/tmp/mut-event/pause_before_new_run"


# However we exit (normal return, exception, SIGTERM), lift every key. A crash
# inside a hold otherwise leaves the stick and R2 down on the console.
@atexit.register
def _release_pad_on_exit():
    try:
        pad._remote_post("/release", {}, timeout=5)
    except Exception:
        pass


os.makedirs(C.RUN_DIR, exist_ok=True)
os.makedirs(C.STUCK_DIR, exist_ok=True)

CURRENT_RECORD = None      # the event card's W-L, e.g. "3-0"
CURRENT_BADGE = None       # ARCADE / COMP, read off the HUD during a play
LOSSES_SEEN = None         # last "N/1 LOSSES" count (only used with HALT_ON_ANY_LOSS)
# Most Feared: the key number of the event whose run is in progress (None =
# pick a new one from the ledger), and the event whose completion has paid a
# next-level key we have not yet read off a reward screen.
CURRENT_EVENT = None
AWAITING_KEY_FROM = None
LAST_PROGRESS = None      # last progress line written to the runs log
# The event whose 3/3 run is being cashed in (its card shows FINISH EVENT and
# its rewards carry "SOURCE: ... #NN COMPLETED"). None outside that walk.
FINISHING = None
SPEND_PENDING = False     # a Join was pressed and the key is not booked yet
KEY_SCROLLS = 0           # left-stick scrolls looking for the key pack
FINISH_WAS_LOSS = False   # the walk is cashing a LOST run (key = refund)
QUIT_PENDING = False      # L4: won enough - quit the next game at once
JUST_QUIT = False         # the run's "loss" is our own planned quit
LAST_COUNTS = {}          # event -> (wins, losses) last seen on its progress

# Set when a screen that only appears after a game (FINISH GAME, RETURN TO HUB,
# the progress screen) is seen. A game is booked only when this is set.
SAW_FINISH_GAME = False
FINAL_SCORE = None         # (theirs, ours) from the end-of-game banner

# The event's own record is the trustworthy result: the in-play score strip
# misreads often. W/L is corrected from the record change between games.
RECORD_AT_LAST_GAME = None
# The row _end_game last wrote. A correction may only ever touch that row.
LAST_ROW_TS = None


# ---------------------------------------------------------------------------
# STATE, HISTORY, ALERTS
# ---------------------------------------------------------------------------

def _save_state():
    try:
        with open(C.STATE_FILE, "w") as fh:
            json.dump({"record": CURRENT_RECORD, "badge": CURRENT_BADGE,
                       "losses_seen": LOSSES_SEEN, "event": CURRENT_EVENT,
                       "awaiting_key_from": AWAITING_KEY_FROM,
                       "finishing": FINISHING,
                       "quit_pending": QUIT_PENDING,
                       # Survives restarts so a re-read progress screen is not
                       # counted twice in the loop record (Oct 16).
                       "last_counts": LAST_COUNTS}, fh)
    except OSError:
        pass


def _load_state():
    global CURRENT_RECORD, CURRENT_BADGE, LOSSES_SEEN
    global CURRENT_EVENT, AWAITING_KEY_FROM, FINISHING, QUIT_PENDING
    try:
        with open(C.STATE_FILE) as fh:
            d = json.load(fh)
        CURRENT_EVENT = d.get("event")
        AWAITING_KEY_FROM = d.get("awaiting_key_from")
        FINISHING = d.get("finishing")
        QUIT_PENDING = bool(d.get("quit_pending"))
        LAST_COUNTS.update({k: tuple(v) for k, v in
                            (d.get("last_counts") or {}).items()})
        CURRENT_RECORD = d.get("record")
        CURRENT_BADGE = d.get("badge")
        LOSSES_SEEN = d.get("losses_seen")
    except (OSError, ValueError):
        pass


_load_state()


def _parse_record(rec):
    try:
        w, l = rec.split("-")
        return int(w), int(l)
    except (AttributeError, ValueError):
        return None


def _patch_last_result(result, why, log=print):
    """Rewrite the result of the row this loop last wrote, and no other."""
    if LAST_ROW_TS is None:
        log(f"    ! record changed ({why}) but this loop has recorded no game "
            f"- NOT touching the history")
        return
    try:
        with open(C.HISTORY) as fh:
            rows = [l for l in fh if l.strip()]
        if not rows:
            return
        last = json.loads(rows[-1])
        if last.get("ts") != LAST_ROW_TS:
            log(f"    ! record changed ({why}) but the last row is not the game "
                f"this loop recorded - NOT touching it")
            return
        if last.get("result") == result:
            return
        log(f"    CORRECTING last game: {last.get('result')} -> {result} ({why})")
        last["result"] = result
        last["result_source"] = "event record"
        last["result_was"] = f"{last.get('theirs')}-{last.get('ours')}"
        rows[-1] = json.dumps(last) + "\n"
        with open(C.HISTORY, "w") as fh:
            fh.writelines(rows)
    except (OSError, ValueError) as e:
        log(f"    ! could not correct last result: {e}")


def _record_game(**row):
    """Append one completed game to the history file. Returns its timestamp.

    The JSONL history is append-only and survives restarts; the log does not.
    A score the validators would reject is written as null, never as a number.
    """
    row.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    row.setdefault("date", time.strftime("%Y-%m-%d"))
    for k in ("ours", "theirs"):
        v = row.get(k)
        if v is not None and (v < 0 or v > C.SCORE_MAX
                              or (v == 1 and not C.ALLOW_SCORE_OF_ONE)):
            row[k + "_rejected"] = v
            row[k] = None
    row.setdefault("record", CURRENT_RECORD)
    row.setdefault("event", CURRENT_EVENT)
    row.setdefault("level", current_level())
    try:
        with open(C.HISTORY, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass
    return row["ts"]


def _record_run(status, event, **extra):
    """One row in the runs log: started, COMPLETE, LOST, key earned."""
    row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "status": status,
           "event": event, "level": keys.level(event) if event else None,
           "record": CURRENT_RECORD, **extra}
    try:
        with open(C.RUNS, "a") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        pass


def _alert(msg, log=print):
    """A clearable alert file a human or a watcher can poll for."""
    try:
        with open(C.ALERT_FILE, "w") as fh:
            fh.write(f"{time.strftime('%H:%M:%S')}  {msg}\n")
    except OSError:
        pass
    log(f"    !!!! ALERT: {msg}")


def _alert_sticky(msg, log=print):
    """An alert only a human clears (health.sh reports each new line)."""
    _alert(msg, log)
    try:
        with open(C.STICKY_ALERT, "a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except OSError:
        pass


def _clear_alert():
    try:
        os.remove(C.ALERT_FILE)
    except OSError:
        pass


def _halt(reason, log=print):
    """Stop the farm and leave a HALT file that farm.sh refuses to start over."""
    try:
        with open(C.HALT_FILE, "w") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {reason}\n")
    except OSError:
        pass
    log(f"    HALT: {reason}")
    _alert_sticky(f"FARM STOPPED - {reason}", log)


def dump_stuck(tag, log=print):
    """Save a screenshot and the screen's OCR text when the loop cannot proceed."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    png = f"{C.STUCK_DIR}/{ts}-{tag}.png"
    try:
        pad.shot(None, png)
        with open(f"{C.STUCK_DIR}/{ts}-{tag}.txt", "w") as fh:
            fh.write(screen.screen_text())
        log(f"    !! STUCK ({tag}) - dumped {png}")
    except Exception as e:
        log(f"    !! STUCK ({tag}) - dump failed: {e}")


# ---------------------------------------------------------------------------
# BETWEEN GAMES
# ---------------------------------------------------------------------------
# These screens have no clock pressure, so they are identified by full-screen
# OCR text rather than by pixel geometry.

def _has(txt, *needles):
    return all(n.upper() in txt for n in needles)


_HOUSE_NUM = re.compile(r"(MURKWATER|CATACOMB|PHANTASMAL|ABYSSAL)\D{0,24}?(\d{2,5})")
_CARD_BUTTONS = ("JOIN EVENT", "CONTINUE", "START", "FINISH EVENT")


def _key_from_digits(digits):
    """'9506' (the title ran into the timer) -> '950'. Longest known prefix."""
    for n in (4, 3, 2):
        if digits[:n] in keys.EVENTS:
            return digits[:n]
    return None


def _cards(txt):
    """[(key, text up to the next card)] in screen order."""
    hits = [(m, _key_from_digits(m.group(2))) for m in _HOUSE_NUM.finditer(txt)]
    hits = [(m, k) for m, k in hits if k]
    out = []
    for j, (m, k) in enumerate(hits):
        end = hits[j + 1][0].start() if j + 1 < len(hits) else len(txt)
        out.append((k, txt[m.end():end]))
    return out


def current_level():
    return keys.level(CURRENT_EVENT) if CURRENT_EVENT else None


LEADING = False   # set by the play loop from the confirmed score


def trailing_key():
    lv = current_level() or 1
    if lv in C.OFFENSE_TRAILING_BY_LEVEL:
        return C.OFFENSE_TRAILING_BY_LEVEL[lv]
    return (C.OFFENSE_TRAILING_HI if lv >= C.OFFENSE_HI_FROM_LEVEL
            else C.OFFENSE_TRAILING)


def current_offense():
    """Not winning: jet (L1) / inside zone (L2+); winning: FB dive."""
    return C.PLAYS[C.OFFENSE_LEADING if LEADING else trailing_key()]


def enter_event(log=print, tries=24):
    """In the MF events list, focus the card for our key and press X.

    The target is the run in progress (CURRENT_EVENT) or, for a new run, the
    highest-level key in the ledger. Only the focused card shows its buttons,
    so the card is found by its number and walked to with up/down - never by
    list position, never by pressing X on whatever happens to be focused.
    """
    global CURRENT_EVENT, CURRENT_RECORD, RECORD_AT_LAST_GAME, FINISHING
    global AWAITING_KEY_FROM, FINISH_WAS_LOSS
    global SPEND_PENDING
    if AWAITING_KEY_FROM and not FINISHING:
        dump_stuck("key-unread", log)
        _halt(f"#{AWAITING_KEY_FROM} was completed but the next-level key it "
              f"paid was never read off a reward screen. Check UT, then "
              f"`keys.py add <key> 1`, clear awaiting_key_from in "
              f"{C.STATE_FILE}, rm {C.HALT_FILE}", log)
        return False
    target = FINISHING or CURRENT_EVENT or keys.pick_next()
    if not target:
        _halt("NO KEYS LEFT - buy L1 keys, then `keys.py add <key> <n>`", log)
        return False
    ti = keys.ORDER.index(target)
    log(f"    events: target {keys.label(target)}"
        + (" (FINISH EVENT)" if FINISHING else
           " (run in progress)" if CURRENT_EVENT else " (new run)"))
    for _ in range(tries):
        txt = screen.screen_text()
        if txt and not any(w in txt for w in C.MF_LIST_WORDS):
            log("    events: this is not the Most Feared list -> back")
            pad.press("moon", hold=0.06)
            time.sleep(1.5)
            return True     # advance_menus re-reads the screen
        flat = re.sub(r"\s*\|\s*", " ", txt or "")
        focused = [k for k, d in keys.DESCRIPTIONS.items() if d in flat]
        if len(focused) != 1:
            log(f"    events: focused card unreadable ({focused}) -> down")
            pad.press("down", hold=0.06)
            time.sleep(0.9)
            continue
        fk = focused[0]
        at = flat.find(keys.DESCRIPTIONS[fk])
        window = flat[at:at + 600]
        if fk != target:
            fi = keys.ORDER.index(fk)
            pad.press("down" if fi < ti else "up", hold=0.06)
            time.sleep(0.9)
            continue
        button = next((b for b in _CARD_BUTTONS if b in window), None)
        if FINISHING and button != "FINISH EVENT":
            # Already cashed in (or never completed): leave the walk.
            log(f"    events: {keys.label(fk)} shows {button}, not FINISH "
                f"EVENT - finishing walk over")
            if AWAITING_KEY_FROM:
                # No key pack was found on the way (e.g. a loss that pays no
                # key). Not worth stopping the night: log it and move on.
                _alert_sticky(f"#{FINISHING}: finish walk ended with no key "
                              f"booked - check UT for an unopened key pack",
                              log)
                _record_run("no key booked", FINISHING)
                AWAITING_KEY_FROM = None
            FINISH_WAS_LOSS = False
            FINISHING = None
            _save_state()
            target = CURRENT_EVENT or keys.pick_next()
            if not target:
                _halt("NO KEYS LEFT - buy L1 keys, then "
                      "`keys.py add <key> <n>`", log)
                return False
            ti = keys.ORDER.index(target)
            continue
        if button == "FINISH EVENT" and not FINISHING:
            # A completed run we did not see end (e.g. a restart): cash it.
            FINISHING = fk
            # Unknown whether it was won or lost: always look for a key pack
            # (an L4 LOSS pays an L3 key; an L4 win has none - see _mf_rewards).
            AWAITING_KEY_FROM = fk
            _save_state()
            log(f"    events: {keys.label(fk)} shows FINISH EVENT - cashing it")
        if button == "FINISH EVENT" and C.PAUSE_AT_LEVEL_END:
            dump_stuck("finish-event-card", log)
            _halt(f"{keys.label(fk)} card shows FINISH EVENT - stopped with "
                  f"NOTHING pressed. Walk it by hand.", log)
            return False
        if button is None:
            log(f"    events: {keys.label(fk)} focused but no button read - "
                f"looking again")
            time.sleep(1.0)
            continue
        r = re.search(r"RECORD\s*:?\s*(\d+)\s*[-–—]\s*(\d+)", window)
        new_rec = f"{r.group(1)}-{r.group(2)}" if r else None
        before = _parse_record(RECORD_AT_LAST_GAME)
        after = _parse_record(new_rec)
        if before and after:
            dw, dl = after[0] - before[0], after[1] - before[1]
            if dw == 1 and dl == 0:
                _patch_last_result("W", f"record {RECORD_AT_LAST_GAME} -> "
                                   f"{new_rec}", log)
            elif dl == 1 and dw == 0:
                _patch_last_result("L", f"record {RECORD_AT_LAST_GAME} -> "
                                   f"{new_rec}", log)
        RECORD_AT_LAST_GAME = new_rec
        CURRENT_RECORD = new_rec
        if CURRENT_EVENT == target and button == "JOIN EVENT":
            # Our run is over without us seeing it end (a loss played by
            # hand, or one missed): the key is gone. Pick the next event.
            log(f"    events: {keys.label(target)} shows JOIN EVENT - the run "
                f"ended unseen (loss?) - moving on")
            _record_run("ended unseen", target, card=button)
            _alert_sticky(f"run {keys.label(target)} ended unseen (card shows "
                          f"JOIN EVENT) - check the result", log)
            CURRENT_EVENT = None
            _save_state()
            target = keys.pick_next()
            if not target:
                _halt("NO KEYS LEFT - buy L1 keys, then "
                      "`keys.py add <key> <n>`", log)
                return False
            ti = keys.ORDER.index(target)
            log(f"    events: new target {keys.label(target)}")
            continue
        if (button == "CONTINUE" and keys.level(target) > keys.max_level()):
            # Above the overnight cap (L1/L2 only): never play it unattended.
            dump_stuck("above-max-level", log)
            _halt(f"{keys.label(target)} run is in progress but the farm is "
                  f"capped at LV{keys.max_level()} - left for the user. To "
                  f"move on: clear 'event' in {C.STATE_FILE}, rm {C.HALT_FILE}",
                  log)
            return False
        if button == "FINISH EVENT":
            log(f"    events: {keys.label(target)} -> FINISH EVENT")
            pad.press("cross", hold=0.06)
            time.sleep(3.0)
            return True
        if CURRENT_EVENT != target and os.path.exists(PAUSE_NEW_RUN):
            # Requested stop BEFORE joining a new run (practise the
            # quit menu first). Nothing pressed on the card.
            _halt(f"paused before a NEW run ({keys.label(target)}) - "
                  f"{PAUSE_NEW_RUN} present. rm it to continue.", log)
            return False
        if CURRENT_EVENT != target:
            # A new run. The key is booked on the key picker (which shows how
            # many we own) or, with one key and no picker, on the spend
            # confirm.
            CURRENT_EVENT = target
            SPEND_PENDING = True
            LAST_COUNTS.pop(target, None)   # a new run starts its count at 0-0
            _record_run("started", target)
        _save_state()
        log(f"    events: {keys.label(target)} -> {button}"
            + (f" (record {CURRENT_RECORD})" if CURRENT_RECORD else ""))
        pad.press("cross", hold=0.06)
        time.sleep(2.5)
        return True
    dump_stuck("enter_event", log)
    _alert_sticky(f"could not focus the {keys.label(target)} card", log)
    return False


def _progress_screen(txt, log):
    """The event progress screen ("x/1 LOSSES", "x/10 WINS"). It only appears
    after a game, so it is a game boundary. Returns False to stop the loop."""
    global SAW_FINISH_GAME, LOSSES_SEEN
    if re.search(r"\d\s*/\s*\d+\s*WINS", txt) and "LOSSES" in txt:
        if not SAW_FINISH_GAME:
            log("    progress screen = game boundary (no RETURN TO HUB was shown)")
        SAW_FINISH_GAME = True
    global CURRENT_EVENT, AWAITING_KEY_FROM, FINISHING, FINISH_WAS_LOSS
    global JUST_QUIT, LAST_COUNTS
    # OCR reads the 0 in "0/1 LOSSES" as O or D (Oct 16 02:57).
    txt = re.sub(r"(?<![A-Z0-9])[ODQ](?=\s*/\s*\d)", "0", txt)
    m_w = re.search(r"(\d+)\s*/\s*(\d+)\s*WINS", txt)
    m_l = re.search(r"(\d+)\s*/\s*(\d+)\s*LOSS", txt)
    global LAST_PROGRESS
    global QUIT_PENDING
    if (CURRENT_EVENT and keys.level(CURRENT_EVENT) == 4 and m_w and m_l
            and int(m_w.group(1)) >= C.L4_QUIT_AFTER_WINS
            and m_w.group(1) != m_w.group(2) and m_l.group(1) == "0"
            and not QUIT_PENDING):
        QUIT_PENDING = True
        _save_state()
        log(f"    *** L4 {m_w.group(1)} WIN(S) - will QUIT the next game to "
            f"collect candy + an L3 key ***")
        _record_run("quit armed", CURRENT_EVENT, progress=m_w.group(0))
    if CURRENT_EVENT and m_w and m_l:
        prog = f"{m_w.group(1)}/{m_w.group(2)} W, {m_l.group(1)}/{m_l.group(2)} L"
        # ⛔ Keyed by event: an L4 "0/3 W, 1/1 L" followed by an L3 game-1
        # loss reads identically, and the L3 loss went unrecorded (Oct 17).
        if (CURRENT_EVENT, prog) != LAST_PROGRESS:
            # L3/L4 loop record: one game per new progress screen. A loss on
            # L4 right after our planned quit is not a real loss.
            _lv = keys.level(CURRENT_EVENT)
            if _lv >= 3:
                _w, _l = int(m_w.group(1)), int(m_l.group(1))
                _pw, _pl = LAST_COUNTS.get(CURRENT_EVENT, (0, 0))
                if _w > _pw:
                    keys.record_result(_lv, "W", f"#{CURRENT_EVENT} {prog}")
                    if _lv == 4:
                        keys.add_candy("L4_win", f"#{CURRENT_EVENT} {prog}")
                    elif _w == int(m_w.group(2)):
                        keys.add_candy("L3_complete", f"#{CURRENT_EVENT} {prog}")
                elif _l > _pl and not JUST_QUIT:
                    keys.record_result(_lv, "L", f"#{CURRENT_EVENT} {prog}")
                LAST_COUNTS[CURRENT_EVENT] = (_w, _l)
            LAST_PROGRESS = (CURRENT_EVENT, prog)
            log(f"    RUN PROGRESS {keys.label(CURRENT_EVENT)}: {prog}")
            _record_run("progress", CURRENT_EVENT, progress=prog)
    if CURRENT_EVENT and m_w and m_w.group(1) == m_w.group(2) != "0":
        log(f"    *** LEVEL COMPLETE: {keys.label(CURRENT_EVENT)} "
            f"{m_w.group(0)} ***")
        _record_run("COMPLETE", CURRENT_EVENT, progress=m_w.group(0))
        if not C.PAUSE_AT_LEVEL_END:
            FINISHING = CURRENT_EVENT
            if keys.level(CURRENT_EVENT) < 4:
                AWAITING_KEY_FROM = CURRENT_EVENT
            CURRENT_EVENT = None
            _save_state()
            log(f"    -> FINISH EVENT walk for #{FINISHING}")
        elif C.PAUSE_AT_LEVEL_END:
            dump_stuck("level-complete", log)
            _halt(f"LEVEL COMPLETE {keys.label(CURRENT_EVENT)} "
                  f"({m_w.group(0)}) - stopped on the progress screen with "
                  f"NOTHING pressed. Walk the rewards / FINISH EVENT by hand "
                  f"to see where the next-level key appears.", log)
            # The run is over; remember it so a later start does not try to
            # continue it, and so the key is still awaited.
            if keys.level(CURRENT_EVENT) < 4:
                AWAITING_KEY_FROM = CURRENT_EVENT
            CURRENT_EVENT = None
            _save_state()
            return False
    elif CURRENT_EVENT and m_l and m_l.group(1) == m_l.group(2) != "0":
        log(f"    *** RUN LOST: {keys.label(CURRENT_EVENT)} {m_l.group(0)} "
            f"- the key is gone ***")
        if JUST_QUIT:
            # Our own L4 win-one-and-quit: expected, not an alarm.
            log(f"    (planned quit after an L4 win - not a real loss)")
            _record_run("QUIT (planned)", CURRENT_EVENT, progress=m_l.group(0))
            JUST_QUIT = False
        else:
            _alert_sticky(f"RUN LOST on {keys.label(CURRENT_EVENT)}", log)
            _record_run("LOST", CURRENT_EVENT, progress=m_l.group(0))
        # A loss pays a key one level down (L3 loss -> L2 key, Oct 15): cash
        # the card with FINISH EVENT and open the key pack like a completion.
        FINISHING = CURRENT_EVENT
        AWAITING_KEY_FROM = CURRENT_EVENT
        FINISH_WAS_LOSS = True
        log(f"    -> FINISH EVENT walk for #{FINISHING} (consolation key?)")
        CURRENT_EVENT = None
        _save_state()
    if "YOUR REWARDS" in txt or AWAITING_KEY_FROM:
        try:
            d = f"{C.RUN_DIR}/rewards"
            os.makedirs(d, exist_ok=True)
            pad.shot(None, f"{d}/{time.strftime('%H%M%S')}.png")
            log("    [reward screen captured for review]")
        except Exception:
            pass
    m_loss = re.search(r"(\d)\s*/\s*(\d)\s*LOSS", txt)
    if m_loss and C.HALT_ON_ANY_LOSS:
        # This is the last screen before the next game starts, so it is where
        # a loss has to be caught. LOSSES_SEEN is persisted so that restarting
        # after a halt does not halt again on the same loss.
        n = int(m_loss.group(1))
        if n > (LOSSES_SEEN or 0):
            LOSSES_SEEN = n
            _save_state()
            _halt(f"LOSS - progress screen reads {m_loss.group(0)}. Farm "
                  f"STOPPED before entering another game. To play on (a fresh "
                  f"run): rm {C.HALT_FILE} and ./farm.sh", log)
            return False
        LOSSES_SEEN = n
        _save_state()
    elif m_loss and LOSSES_SEEN is not None:
        LOSSES_SEEN = None
        _save_state()
    log(f"    rewards/progress -> advancing  |RAW| {txt[:160]}")
    # Once only during a finishing walk: a second X could land on the list.
    for _ in range(1 if FINISHING else 2):
        pad.press("cross", hold=0.06)
        time.sleep(0.7)
    return True


# The digits on the opened card are in a display font OCR misreads as letters
# ("100" -> "TOO", Oct 16): accept look-alikes and map them back to digits.
_KEY_CARD = re.compile(r"(?<![A-Z0-9])([0-9TILODQSZB]{2,4})\s*"
                       r"(MURKWATER|CATACOMB|PHANTASMAL|ABYSSAL)")
_DIGIT_LOOKALIKE = str.maketrans("TILODQSZB", "111000528")


def _mf_rewards(txt, log):
    """One YOUR REWARDS screen. Returns False to stop.

    Only rewards whose SOURCE names the event being finished are ours; the
    rest (Breakouts level-ups, etc.) are advanced past, never opened. On L1-3
    our rewards hold "Most Feared Level N CPU Event Key" next to the candy:
    scroll to it (left stick) and open it (square). L4: advance through all.
    """
    global KEY_SCROLLS, FINISHING, AWAITING_KEY_FROM
    _src = re.search(r"SOURCE:?\s*([^|]*)", txt)
    ours = FINISHING and _src and re.search(rf"#\s*{FINISHING}(?!\d)",
                                            _src.group(1))
    if ours and AWAITING_KEY_FROM:
        if "EVENT KEY" in txt and "OPEN PACK" in txt:
            log(f"    rewards: key pack from #{FINISHING} -> OPEN (square)")
            KEY_SCROLLS = 0
            pad.press("box", hold=0.06)
            time.sleep(5.0)
            return True
        if KEY_SCROLLS < 4:
            KEY_SCROLLS += 1
            log(f"    rewards: #{FINISHING} reward, key pack not focused -> "
                f"scroll right ({KEY_SCROLLS})")
            pad.press("lstick_right", hold=0.06)
            time.sleep(2.0)
            return True
        if keys.level(FINISHING) == 4:
            # L4 WIN: no key in its rewards (candy + the player pack, which
            # the user opens). Stop looking and advance.
            log(f"    rewards: #{FINISHING} has no Event Key pack - an L4 win; "
                f"advancing, player pack left unopened")
            AWAITING_KEY_FROM = None
            KEY_SCROLLS = 0
            _save_state()
        else:
            dump_stuck("key-pack-missing", log)
            _halt(f"#{FINISHING} rewards are up but no Event Key pack was "
                  f"found after {KEY_SCROLLS} scrolls - nothing advanced", log)
            return False
    src = re.search(r"SOURCE:?\s*([^|]*)", txt)
    log(f"    rewards: advancing past {src.group(1).strip()[:50] if src else '?'}"
        + ("" if ours else " (not ours)"))
    if ours and keys.level(FINISHING) == 4:
        log(f"    rewards: #{FINISHING} final-house rewards -> advancing; "
            f"the user opens the player pack")
        _record_run("final rewards collected", FINISHING)
        FINISHING = None
        _save_state()
    pad.press("cross", hold=0.06)
    time.sleep(2.5)
    return True


def _mf_opened_key(txt, log):
    """The opened key pack: "Trick-or-Treat Key / 90 CATACOMB CIRCLE".
    Book the key, close with circle. Returns False to stop."""
    global AWAITING_KEY_FROM, FINISHING, KEY_SCROLLS, FINISH_WAS_LOSS
    m = _KEY_CARD.search(txt)
    k = _key_from_digits(m.group(1).translate(_DIGIT_LOOKALIKE)) if m else None
    if not k or keys.EVENTS[k][1] != m.group(2) + {
            "MURKWATER": " CROSSING", "CATACOMB": " CIRCLE",
            "PHANTASMAL": " PLACE", "ABYSSAL": " PARKWAY"}[m.group(2)]:
        dump_stuck("key-unreadable", log)
        _halt(f"opened a key pack but could not read the key "
              f"({m.group(0) if m else 'no match'}) - left on screen", log)
        return False
    if AWAITING_KEY_FROM:
        why = (f"refund for losing #{AWAITING_KEY_FROM}" if FINISH_WAS_LOSS
               else f"earned completing #{AWAITING_KEY_FROM}")
        left = keys.change(k, +1, why)
        log(f"    *** KEY {'REFUNDED' if FINISH_WAS_LOSS else 'EARNED'}: "
            f"{keys.label(k)} (now {left}) ***")
        _record_run("key refunded" if FINISH_WAS_LOSS else "key earned",
                    AWAITING_KEY_FROM, key=k)
        FINISH_WAS_LOSS = False
        AWAITING_KEY_FROM = None
        FINISHING = None
        KEY_SCROLLS = 0
        _save_state()
    else:
        log(f"    opened key {keys.label(k)} with no key awaited - NOT booked")
    pad.press("moon", hold=0.06)
    time.sleep(2.5)
    return True


def _quit_row_lit():
    """Brightness of the QUIT GAME row vs the rest of the pause list.
    The focused row is a light box (~180); others are dark (~25-35)."""
    import Quartz  # noqa: F401  (screen/actions already load it)
    shot = f"{C.SHOT_DIR}/_pause.png"
    pad.shot(None, shot)
    rows = actions._rows_from(shot, {"QUIT": {"want": ("QUIT GAME",), "not": ()}})
    return max((r["bright"] for r in rows), default=0)


def _focus_quit_game(log, tries=10):
    """UP until QUIT GAME is the lit row (UP from RESUME wraps straight to it;
    from anywhere else UP walks up and wraps). Confirmed twice."""
    for i in range(tries):
        b = _quit_row_lit()
        if b >= 120:
            time.sleep(0.4)
            if _quit_row_lit() >= 120:
                log(f"    pause menu: QUIT GAME focused (bright {b:.0f})")
                return True
        pad.press("up", hold=0.06)
        time.sleep(0.6)
    return False


def _l4_quit(log):
    """Pause -> QUIT GAME -> "YES - EXIT TO MENU" (L4 win-one-and-quit).

    Walked by hand Oct 16 17:35 on a #900 practice run (frames calib/q1-q3):
      OPTIONS -> pause menu, RESUME focused, QUIT GAME last; UP once WRAPS to
      QUIT GAME -> X -> popup "Quitting will give you a loss in this event",
      rows "YES - EXIT TO MENU" / "NO", NO FOCUSED BY DEFAULT -> select YES ->
      straight to YOUR PROGRESS (1/1 LOSSES) -> the normal Finish Event walk.
    Returns False to stop the loop.
    """
    global QUIT_PENDING
    log("    L4: QUIT this game (win-one-and-quit) -> pause")
    pad.press("options", hold=0.1)
    time.sleep(2.0)
    if "QUIT GAME" not in screen.screen_text():
        dump_stuck("l4-quit-no-pause", log)
        _halt("L4 quit: pause menu did not show QUIT GAME - left as is", log)
        return False
    if not _focus_quit_game(log):
        dump_stuck("l4-quit-select", log)
        _halt("L4 quit: could not put focus on QUIT GAME", log)
        return False
    pad.press("cross", hold=0.06)
    time.sleep(1.5)
    txt = screen.screen_text()
    if "EXIT TO MENU" not in txt:
        dump_stuck("l4-quit-unknown", log)
        _halt(f"L4 quit: no 'YES - EXIT TO MENU' popup: {txt[:80]}", log)
        return False
    ok, detail = actions.select_row(
        {"YES": {"want": ("EXIT TO MENU",), "not": ()},
         "NO": {"want": ("NO",), "not": ("EXIT",)}},
        "YES", "quit confirm", log=log)
    if not ok:
        dump_stuck("l4-quit-confirm", log)
        _halt(f"L4 quit: could not confirm YES ({detail})", log)
        return False
    global JUST_QUIT
    QUIT_PENDING = False
    JUST_QUIT = True
    _save_state()
    _record_run("quit", CURRENT_EVENT)
    log("    L4: game quit -> YOUR PROGRESS (1/1 L) -> Finish Event walk")
    time.sleep(3.0)
    return True


def advance_menus(log=print, tries=25):
    """Walk postgame -> hub -> event -> entry options -> back into a game.

    Returns True once a playcall screen is up again.
    """
    global SAW_FINISH_GAME, FINAL_SCORE, SPEND_PENDING
    unknown_presses = 0
    loading_waits = 0
    hub_moves = 0
    # Start from a neutral pad: a key-up lost to a focus change would otherwise
    # scroll every menu below.
    try:
        pad.release()
    except Exception:
        pass
    for _ in range(tries):
        side, _i, _name, ratio = screen.read_both()
        if side is not None and ratio >= C.CONFIDENT:
            try:
                _q, _g, ps = screen.read_clocks()
            except Exception:
                ps = None
            if ps is not None:
                log(f"    playcall screen is back (play clock :{ps})")
                if ps <= 6:
                    log(f"    ! only {ps}s of play clock at handover - a delay "
                        f"of game may be unavoidable")
            else:
                log("    playcall screen is back")
            return True

        txt = screen.screen_text()
        if not txt:
            time.sleep(1.5)
            continue

        if _has(txt, "FINISH GAME"):
            # Madden's own end-of-game menu. Record the boundary and the banner
            # score before pressing anything.
            SAW_FINISH_GAME = True
            fs = hud.read_final_score()
            if fs:
                FINAL_SCORE = fs
                log(f"    end of game: FINAL {fs[0]}-{fs[1]} (from the banner)")
            else:
                log("    end of game: banner score unreadable")
            ok, detail = actions.choose_finish_game(log=log)
            if not ok:
                dump_stuck("finish-game", log)
                _alert_sticky(f"END OF GAME: could not confirm FINISH GAME "
                              f"({detail}). Screen dumped to {C.STUCK_DIR}.", log)
                return False
            log(f"    end of game -> {detail}")

        elif _has(txt, "RETURN TO HUB"):
            # This event's usual game boundary (not on the pause menu).
            SAW_FINISH_GAME = True
            fs = hud.read_final_score()
            if fs:
                FINAL_SCORE = fs
                log(f"    postgame: RETURN TO HUB - FINAL {fs[0]}-{fs[1]} "
                    f"(from the banner)")
            else:
                log("    postgame: RETURN TO HUB - banner score unreadable")
            pad.press("cross", hold=0.06)
            time.sleep(2.2)

        elif _has(txt, "SELECT GAME MODE"):
            log("    events list")
            if not enter_event(log=log):
                return False

        elif re.search(r"(?<![A-Z])LOCKED\b", txt) and (
                _has(txt, "ACCESS RULES") or _has(txt, "HAVE AT LEAST")):
            # The chosen lineup mode is closed to this squad (Oct 1: Lineup
            # Restricted needs 3x 85+ OVR BO players). Retrying cannot fix
            # it - stop and say why. The popup is left up for the user.
            dump_stuck("lineup-locked", log)
            _halt(f"{C.LINEUP_WANT} lineup is LOCKED - access rules not "
                  f"met. Fix the lineup or set MUT_EVENT_LINEUP=FREE", log)
            return False

        elif _has(txt, "WANT TO SPEND THIS ITEM"):
            # The key picker's confirm popup. YES is the default focus.
            ok, detail = actions.select_row(
                {"YES": {"want": ("YES",), "not": ()},
                 "CANCEL": {"want": ("CANCEL",), "not": ()}},
                "YES", "spend key", log=log)
            if not ok:
                dump_stuck("spend-confirm", log)
                _alert_sticky(f"could not confirm spending the key ({detail})",
                              log)
                return False
            log(f"    spend key -> {detail}")
            if SPEND_PENDING and CURRENT_EVENT:
                left = keys.change(CURRENT_EVENT, -1,
                                   "spent entering (no picker - single key)")
                log(f"    KEY #{CURRENT_EVENT} SPENT -> {left} left")
                _record_run("key spent", CURRENT_EVENT, keys_left=left)
            SPEND_PENDING = False
            time.sleep(2.5)

        elif _has(txt, "ITEMS FOUND") and _has(txt, "APPLY TO SET"):
            # Most Feared key picker: every key of this number we own, as
            # cards ("8 ITEMS FOUND"). X applies one = spends it. The count
            # is the truth: sync the ledger to it.
            # The card font reads 1 as T/I and 0 as O ("T ITEMS FOUND",
            # "OO PHANTASMAL PLACE" for 1x #100, Oct 16): map look-alikes.
            m = re.search(r"(?<![A-Z0-9])([0-9TILODQSZB]{1,3})\s*ITEMS? FOUND", txt)
            n = int(m.group(1).translate(_DIGIT_LOOKALIKE)) if m else None
            want = CURRENT_EVENT
            house = keys.EVENTS[want][1].split()[0] if want else ""
            mk = re.search(rf"(?<![A-Z0-9])([0-9TILODQSZB]{{0,4}})\s*{house}", txt) \
                if want else None
            got = mk.group(1).translate(_DIGIT_LOOKALIKE) if mk else None
            # ⛔ Reject only a read that is clearly ANOTHER key's number. The
            # card font garbles digits ("150" read as "60", Oct 17) and the
            # picker only ever lists keys this event accepts.
            if not want or mk is None or (got in keys.EVENTS and got != want):
                dump_stuck("key-picker-mismatch", log)
                _alert_sticky(f"key picker does not show #{want} keys - "
                              f"backing out", log)
                pad.press("moon", hold=0.06)
                time.sleep(1.5)
                return False
            if n is not None:
                keys.set_count(want, n - 1, f"spent entering (picker showed {n})")
                SPEND_PENDING = False
                log(f"    KEY #{want} SPENT - picker showed {n}, {n - 1} left")
                _record_run("key spent", want, owned_before=n)
            pad.press("cross", hold=0.06)
            time.sleep(2.5)

        elif _has(txt, "ENTRY OPTIONS") and "KEY" in txt:
            # Most Feared: one row, "#950 Murkwater Crossing Key". Only
            # confirm the key for the event we mean to play.
            want = CURRENT_EVENT
            m = re.search(r"#\s*(\d{2,4})\s*[A-Z ]*KEY", txt)
            seen = _key_from_digits(m.group(1)) if m else None
            if want and seen and seen != want:
                dump_stuck("wrong-key", log)
                _alert_sticky(f"ENTRY OPTIONS shows key #{seen} but the run is "
                              f"#{want} - backing out", log)
                pad.press("moon", hold=0.06)
                time.sleep(1.5)
                return False
            log(f"    entry options: key #{seen or '?'} -> X")
            pad.press("cross", hold=0.06)
            time.sleep(2.5)

        elif _has(txt, "ENTRY OPTIONS") or ("LINEUP" in txt and
                                            "RESTRICTED" in txt):
            ok, detail = actions.choose_lineup(log=log)
            if not ok:
                dump_stuck("entry-options", log)
                _alert_sticky(
                    f"ENTRY OPTIONS: could not confirm the {C.LINEUP_WANT} "
                    f"lineup ({detail}). NOT entering. Screen dumped to "
                    f"{C.STUCK_DIR}.", log)
                return False
            log(f"    entry options -> {detail}")

        elif _has(txt, "HOW TO PLAY") or _has(txt, "REQUIREMENTS"):
            log("    intro -> continue")
            pad.press("cross", hold=0.06)
            time.sleep(2.5)

        elif _has(txt, "FINISH EVENT") and C.PAUSE_AT_LEVEL_END:
            dump_stuck("finish-event", log)
            _halt("FINISH EVENT on screen - stopped with NOTHING pressed. "
                  "Walk it by hand to see where the next-level key appears.",
                  log)
            return False

        elif _has(txt, "FINISH EVENT"):
            log("    FINISH EVENT -> collecting rewards")
            pad.press("cross", hold=0.06)
            time.sleep(2.0)

        elif _has(txt, "EVENTS") and _has(txt, "CHALLENGES"):
            # MUT hub, PLAY tab. Only the FOCUSED tile shows its subtitle
            # ("Play MF CPU Events..."), so press X only when that is up.
            if _has(txt, "PLAY MF CPU"):
                log("    MUT hub -> MF: TRICK OR TREAT CPU EVENTS")
                pad.press("cross", hold=0.06)
                time.sleep(2.5)
            else:
                hub_moves += 1
                # Into the tile row, then walk left (the tile is 2nd from the
                # left); past the left edge, walk right.
                key = ("down" if hub_moves == 1 else
                       "left" if hub_moves <= 6 else "right")
                log(f"    MUT hub: MF tile not focused -> {key}")
                pad.press(key, hold=0.06)
                time.sleep(0.8)

        elif _has(txt, "CHIAKI-NG") or _has(txt, "REFRESH PSN HOSTS"):
            # The stream dropped to chiaki's host list. Restart it; never press
            # into the chiaki UI.
            log("    chiaki dropped -> restarting stream")
            try:
                pad.restart_stream()
            except Exception as e:
                log(f"    ! stream restart failed: {e}")
            time.sleep(6.0)

        elif ("SELECT A PLAY" in txt or "COMPLETE ANY REQUIREMENT" in txt) \
                and not any(k in txt for k in ("YOUR REWARDS", "YOUR PROGRESS",
                                               "SELECT GAME MODE")):
            # In-game: the MF playcall overlay or a play/cutscene behind it.
            # Never press here (circle at the line was harmless only by luck).
            log("    in-game overlay (SELECT A PLAY) -> waiting, no press")
            time.sleep(2.0)

        elif "USING THIS CONTROLLER" in txt:
            # PS5 system popup; focus sits on the logged-in user.
            log("    'Who's using this controller?' -> X (logged-in user)")
            pad.press("cross", hold=0.06)
            time.sleep(2.0)

        elif "DISMISS ALL" in txt and ("TRICK-OR-TREAT" in txt or "EVENT KEY" in txt
                                       or "OR-TREAT" in txt or "OR- TREAT" in txt):
            # The opened key card. Matched loosely: OCR read TRICK-OR-TREAT as
            # "IRICK-OR- TRE" (Oct 16 18:40) and the card was closed unread.
            if not _mf_opened_key(txt, log):
                return False

        elif "YOUR REWARDS" in txt and "SOURCE" in txt:
            if not _mf_rewards(txt, log):
                return False

        elif any(k in txt for k in ("YOUR REWARDS", "YOUR PROGRESS", "ADVANCE",
                                    "SCROLL REWARDS", "OBJECTIVE PROGRESS")):
            if not _progress_screen(txt, log):
                return False

        elif len(txt) < 45 or (len(txt) < 140
                               and any(k in txt for k in ("EASPORTS", "MADDEN",
                                                          "SPORTS"))):
            # Loading / transition: almost no text. Wait, never press. The
            # length bound matters: "SPORTS" also appears in the watermark on
            # real screens.
            loading_waits += 1
            # A frozen chiaki feed looks exactly like a static loading screen.
            if loading_waits in (8, 20):
                log("    same screen too long - restarting chiaki (may be frozen)")
                try:
                    pad.restart_stream()
                except Exception as e:
                    log(f"    ! stream restart failed: {e}")
                time.sleep(6.0)
                continue
            if loading_waits % 10 == 0 and not screen.console_up():
                dump_stuck("console-offline", log)
                _alert_sticky("PS5 UNREACHABLE - console likely powered off", log)
                return False
            if loading_waits > 40:
                dump_stuck("loading-forever", log)
                _alert(f"stuck on a loading screen ({loading_waits} waits)", log)
                return False
            log(f"    loading/transition -> waiting ({txt[:40]})")
            time.sleep(3.0)

        else:
            # Unknown screen: back out with circle, never cross. Cross confirms
            # and walks deeper into menus (My Team, packs...).
            if os.path.exists(STOP_FILE):
                log("    STOP requested -> exiting from menus")
                return False
            if "EVENT KEY" in txt or "TRICK-OR" in txt:
                # Never close a key card blind - its number must be read.
                dump_stuck("key-card-unread", log)
                _halt(f"a key card is up but was not recognised: {txt[:120]}",
                      log)
                return False
            unknown_presses += 1
            if unknown_presses > 8:
                dump_stuck("unknown-screen-loop", log)
                _alert(f"lost in unknown menus: {txt[:60]}", log)
                return False
            log(f"    unknown screen, backing out (moon): {txt[:160]}")
            pad.press("moon", hold=0.06)
            time.sleep(1.2)
    return False


# ---------------------------------------------------------------------------
# PER-GAME STATE AND THE SCORE
# ---------------------------------------------------------------------------

class Game:
    """Per-game state, replaced wholesale at every game boundary."""

    def __init__(self, joined=False):
        # `joined`: the loop attached to a game already in progress (every
        # restart does). Its result is real; its duration is not comparable, so
        # it is recorded as partial and excluded from pace statistics.
        self.joined = joined
        self.partial = joined
        self.t0 = time.time()
        self.plays = 0
        self.event = None      # the event this game was played in (set on the first play)
        self.theirs = 0
        self.ours = 0
        self.since_theirs = 0    # plays since each side last read acceptably
        self.since_ours = 0
        self.opp_confirmed = False   # the opponent's score has read at least once
        self.pend = {}           # an out-of-range value awaiting repeats
        self.contra = {}         # a lower value awaiting repeats
        self.score_suspect = False   # our tally has been corrected once
        self.quarter = None
        self.first_quarter = None
        self.seen_q4 = False     # overtime is only possible after Q4
        self.overtime = False
        self.badge = None
        self.need_chew = True    # the chew-clock tempo must be (re-)set
        # The tempo actually set on the console: a fresh game starts on NORMAL;
        # a game joined mid-way is unknown (None) and is set on first offence.
        self.tempo = None if joined else "NORMAL"
        self.last_side = None    # side of the last playcall we acted on
        self.nh_seen = None      # No Huddle: when PREPLAY was first seen
        self.nh_pressed = 0      # No Huddle: X taps sent at this line
        self.fbd_slow = False    # the hybrid dive is on its short run
        self.skips = 0           # consecutive deliberate no-snaps
        self.layout = None       # broadcast presentation, recorded only
        self.clock_x = None
        self.offense_used = None

    @property
    def result(self):
        """W / L / T from the score, or None when the score cannot carry it.
        Equal scores are the most common misread, so a tie needs a confirmed,
        never-corrected score. The event record fixes it up afterwards."""
        if self.ours > self.theirs:
            return "W"
        if self.ours < self.theirs:
            return "L"
        if self.opp_confirmed and not self.score_suspect:
            return "T"
        return None


def _accept_score(g, which, value, log):
    """Validate one side's score before believing it. Returns True to accept.

    - Nothing above SCORE_MAX, and 1 only if ALLOW_SCORE_OF_ONE.
    - A LOWER value (points can be stolen in this event) needs SCORE_CONTRADICT
      identical reads in a row; the tally is then marked suspect.
    - A jump larger than plausible for the plays since the last read needs 3
      identical reads, and a far larger one is never accepted.
    """
    if value == 1 and not C.ALLOW_SCORE_OF_ONE:
        log(f"    ! REJECTED {which} score 1 - impossible in football")
        return False
    if value > C.SCORE_MAX:
        log(f"    ! REJECTED {which} score {value} - above {C.SCORE_MAX}, "
            f"not a football score")
        return False
    have = g.theirs if which == "theirs" else g.ours
    since = g.since_theirs if which == "theirs" else g.since_ours
    if g.joined and not g.opp_confirmed and g.plays == 0:
        # First read of a game we JOINED (a restart): there is no previous
        # score to jump from. Oct 16 08:50: "31" was rejected as impossible
        # from 0, so a 31-5 lead was played as 0-5 (jet, chew off).
        log(f"    {which} score {value} - first read after joining, accepted")
        return True

    if value < have:
        k = which + "_lower"
        if g.contra.get(k) == value:
            g.contra[k + "_n"] = g.contra.get(k + "_n", 0) + 1
        else:
            g.contra[k], g.contra[k + "_n"] = value, 1
        if g.contra[k + "_n"] >= C.SCORE_CONTRADICT:
            log(f"    !! {which} score read as {value} "
                f"{g.contra[k + '_n']}x in a row while we hold {have} - "
                f"OUR VALUE WAS WRONG, correcting to {value}")
            g.score_suspect = True
            g.contra[k] = None
            g.contra[k + "_n"] = 0
            return True
        log(f"    ! REJECTED {which} score {value} - scores never decrease "
            f"(have {have}, seen {g.contra[k + '_n']}x)")
        return False

    room = min(C.SCORE_JUMP_MAX * max(1, since), C.SCORE_JUMP_ABS)
    if value - have > room:
        if g.pend.get(which) == value:
            g.pend[which + "_n"] = g.pend.get(which + "_n", 0) + 1
        else:
            g.pend[which], g.pend[which + "_n"] = value, 1
        slack = room + C.SCORE_JUMP_MAX * 2
        if value - have > slack:
            log(f"    !! REJECTED {which} score {value} as IMPOSSIBLE "
                f"(was {have}, {since} plays ago, max plausible {slack})")
            g.pend[which] = None
            g.pend[which + "_n"] = 0
            return False
        if g.pend[which + "_n"] >= 3:
            log(f"    {which} score {value} confirmed 3x - accepting "
                f"(was {have})")
            g.pend[which] = None
            g.pend[which + "_n"] = 0
            return True
        log(f"    ! REJECTED {which} score {value} "
            f"(last {have}, {since} plays ago)")
        return False
    g.pend[which] = None
    g.pend[which + "_n"] = 0
    return True


# ---------------------------------------------------------------------------
# THE LOOP
# ---------------------------------------------------------------------------

def _special_teams(stamp, log):
    """4th down / kickoff screen. Returns (snapped, status, hint).

    Kickoffs are taken normally. On 4th down the loop never punts and never
    kicks a field goal (GO_FOR_IT_ON_FOURTH): it goes back to the offensive
    FAVORITES and runs the play.
    """
    try:
        sttxt = (screen.screen_text() or "").upper()
    except Exception:
        sttxt = ""
    kick_screen = "KICKOFF" in sttxt or "ONSIDE" in sttxt
    fourth_down = ("PUNT" in sttxt or "FIELD GOAL" in sttxt) and not kick_screen
    if not (fourth_down and C.GO_FOR_IT_ON_FOURTH):
        return actions.call_special_teams(log=log)

    if not actions.goto_tab("offense", C.OFF_TAB):
        # Never fall back to a kick: look again on the next pass.
        return False, "4th down: could not reach FAVORITES - retrying", 0.5
    play = current_offense()
    log(f"[{stamp()}]   4th down: going for it -> {play['name']}")
    pad.press(play["button"], hold=C.OFF_SELECT_HOLD)
    if play.get("settle"):
        time.sleep(play["settle"])
    snapped, status, hint = actions.chew_and_hike(
        play["run_seq"], log=log, allow_skip=False, chew=False)
    return snapped, f"4TH DOWN GO - {status}", hint


def play_loop(max_plays=99999, log=print):
    global CURRENT_BADGE, SAW_FINISH_GAME, FINAL_SCORE, LEADING
    t_run = time.time()
    g = Game(joined=True)      # the first game of any start is one we joined
    game_no = 1
    total_plays = 0
    idle_since = None
    _t_slow = _t_console = _t_waitlog = 0.0
    dead_t0 = None
    last_play_t = time.time()
    ever_played = False        # has a playcall screen been seen at all
    off_plays = 0              # offensive snaps this session - drives the flip
    last_game_t = time.time()
    stuck_dumped = False
    game_stall_alerted = False
    last_situation = None
    same_situation = 0
    last_clock = None
    no_tab_streak = 0          # consecutive "playcall UI but no tab strip" reads

    def stamp():
        el = int(time.time() - t_run)
        return f"{el // 60:02d}:{el % 60:02d}"

    log(f"[{stamp()}] === FARM START ===  "
        f"chew={'ON @ :%d' % C.HIKE_AT if C.HIKE_AT < 99 else 'OFF'}"
        f"  snap_floor={C.MIN_SNAP_WAIT}s")
    log(f"           event={keys.label(CURRENT_EVENT) if CURRENT_EVENT else 'none yet'}"
        f"  keys: {keys.load()}")
    log(f"           offense={C.PLAYS[C.OFFENSE_TRAILING]['name']} (L1) / "
        f"{C.PLAYS[C.OFFENSE_TRAILING_HI]['name']} (L{C.OFFENSE_HI_FROM_LEVEL}+) "
        f"until leading, then {C.PLAYS[C.OFFENSE_LEADING]['name']}"
        + (" (flipped every other play)" if C.FLIP_PLAY and C.OFFENSE.get("flip")
           else "")
        + (f" (short run at +{C.FBD_SLOW_LEAD})" if C.OFFENSE.get("run_seq")
           else "")
        + f"  defense={C.DEF_PLAY_NAME}"
        + (" [macro]" if C.DEF_MACRO else ""))

    while total_plays < max_plays:
        # Stop requests (farm.sh stop) are honoured between plays, never
        # mid-play: a kill between the snap and the run leaves the down to the
        # CPU.
        if os.path.exists(STOP_FILE):
            try:
                os.remove(STOP_FILE)
            except OSError:
                pass
            log(f"[{stamp()}]   STOP requested -> exiting at a play boundary")
            return total_plays
        if os.path.exists(C.HALT_FILE):
            log(f"[{stamp()}]   HALT file present -> exiting the loop")
            return total_plays

        _t_iter = time.time()
        side, i, name, ratio = screen.read_both()

        # FROZEN FEED. Only trusted while a game is live (menus are static),
        # and confirmed once more before paying for a stream restart.
        in_play_recently = (ever_played
                            and (time.time() - last_play_t) < C.FROZEN_ONLY_WITHIN)
        if in_play_recently and screen.FROZEN_STREAK >= C.FROZEN_RESTART:
            before = screen.FROZEN_STREAK
            try:
                pad._remote_post("/focus", {})
            except Exception:
                pass
            time.sleep(C.FROZEN_CONFIRM_SECS)
            try:
                screen.read_both()
            except Exception:
                pass
            if screen.FROZEN_STREAK < before:
                log(f"[{stamp()}]   feed moved again after {before} identical "
                    f"frames - NOT restarting")
                continue
            log(f"[{stamp()}]   FROZEN FEED ({screen.FROZEN_STREAK} identical "
                f"frames) -> restarting chiaki")
            try:
                pad.restart_stream()
            except Exception as e:
                log(f"    ! stream restart failed: {e}")
            time.sleep(6.0)
            if screen.FROZEN_STREAK >= C.FROZEN_ALERT:
                dump_stuck("frozen-feed", log)
                _alert("feed frozen and a chiaki restart did not clear it", log)
            continue

        # -------------------------------------------------------------------
        # NOT ON A PLAYCALL SCREEN
        # -------------------------------------------------------------------
        if side is None or ratio < C.CONFIDENT:
            # ⭐ NO HUDDLE (L4 test): our offence goes straight back to the
            # line - no playcall screen. When the last thing we called was
            # OFFENCE and the line's PREPLAY prompt is up, tap X until it snaps.
            # A defensive playcall in between resets last_side, so this can
            # never fire on defence.
            if (C.NH_LINE_TAPS and g.tempo == "NO HUDDLE"
                    and g.last_side == "offense"):
                try:
                    _bar = screen.action_bar()
                except Exception:
                    _bar = ""
                if "PREPLAY" in _bar:
                    now = time.time()
                    if g.nh_seen is None:
                        g.nh_seen = now
                        log(f"[{stamp()}]   NO HUDDLE: at the line")
                    due = g.nh_seen + C.NH_SNAP_FIRST + g.nh_pressed * C.NH_SNAP_EVERY
                    if now >= due and g.nh_pressed < 4:
                        pad._remote_post("/focus", {})
                        time.sleep(0.2)
                        pad._remote_post("/press", {"buttons": ["cross"],
                                                    "hold": C.OFF_SNAP_HOLD,
                                                    "gap": 0.0, "focus": False})
                        g.nh_pressed += 1
                        log(f"[{stamp()}]   NO HUDDLE: snap tap {g.nh_pressed} "
                            f"(+{now - g.nh_seen:.1f}s at the line)")
                    idle_since = None
                    last_play_t = time.time()
                    time.sleep(0.3)
                    continue
                elif g.nh_seen is not None:
                    # PREPLAY gone after our taps: the play is live.
                    log(f"[{stamp()}]   NO HUDDLE: snapped after "
                        f"{g.nh_pressed} tap(s), {time.time() - g.nh_seen:.1f}s "
                        f"at the line -> {current_offense()['name']}")
                    total_plays += 1
                    g.plays += 1
                    g.event = g.event or CURRENT_EVENT
                    off_plays += 1
                    g.nh_seen, g.nh_pressed = None, 0
                    idle_since = None
                    last_play_t = time.time()
                    time.sleep(0.3)
                    continue
            if idle_since is None:
                idle_since = time.time()
            idle_secs = time.time() - idle_since
            if time.time() - _t_waitlog >= 20:
                _t_waitlog = time.time()
                log(f"  ... waiting ({name}, ratio {ratio:.2f})")

            # Console reachability: the one failure a screen cannot show.
            if time.time() - _t_console >= 40:
                _t_console = time.time()
                if not screen.console_up():
                    dump_stuck("console-offline", log)
                    _alert_sticky("PS5 UNREACHABLE - console likely powered off",
                                  log)
                    time.sleep(30)
                    continue

            # A loop that keeps pressing but never finishes a game.
            if (not game_stall_alerted
                    and (time.time() - last_game_t) > C.GAME_STALL_SECS):
                game_stall_alerted = True
                mins = (time.time() - last_game_t) / 60
                dump_stuck("game-stall", log)
                _alert(f"no game completed in {mins:.0f} min - check if stuck", log)

            if not stuck_dumped and (time.time() - last_play_t) > 300:
                stuck_dumped = True
                dump_stuck("no-play-5min", log)
                log(f"[{stamp()}]   watchdog: backing out to find a known screen")
                for _ in range(3):
                    pad.press("moon", hold=0.06)
                    time.sleep(1.2)
                advance_menus(log=log)
                idle_since = None
                continue

            # A playcall sub-screen: the action bar is there but the tab strip
            # is genuinely absent (not just a marginal read). "NO-TABSTRIP"
            # means a live play, which needs no checks.
            if name != "NO-TABSTRIP":
                has_tabs, _txt = screen.has_tab_words()
                if not has_tabs:
                    bar = screen.action_bar()
                    if any(k in bar for k in ("ADD/REMOVE FAVORITE",
                                              "COACH ADJUSTMENTS", "FLIP PLAY",
                                              "PLAYCALL SUBSTITUTIONS")):
                        # ⛔ Usually just a slow tab read on the playcall screen
                        # itself (Oct 16): a circle there backed out of our own
                        # selection and Madden's default play (JET SWEEP) got
                        # snapped. Look again; only back out when it persists.
                        no_tab_streak += 1
                        if no_tab_streak < 3:
                            time.sleep(0.6)
                            continue
                        log(f"[{stamp()}]   playcall UI, NO tab strip x"
                            f"{no_tab_streak} -> backing out of sub-screen (moon)")
                        no_tab_streak = 0
                        pad.press("moon", hold=0.06)
                        time.sleep(0.9)
                        idle_since = None
                        continue

            # Full-screen OCR, rate-limited.
            if time.time() - _t_slow >= C.SLOW_PATH_EVERY:
                _t_slow = time.time()
                txt = screen.screen_text()
                # If chiaki is not frontmost the daemon captures the Mac
                # desktop, and every press would go to whatever app has focus.
                if screen.looks_like_mac(txt):
                    dump_stuck("mac-desktop", log)
                    _alert_sticky(
                        "CAPTURING THE MAC DESKTOP, NOT THE PS5 - chiaki is not "
                        "frontmost. STOPPING: every press would go to whatever "
                        "app has focus. Bring chiaki back up, then restart.", log)
                    log(f"[{stamp()}] === ABORTING: not looking at the console ===")
                    return total_plays
                # A visible play clock off the playcall screen means a play is
                # selected at the line and nobody has snapped it.
                try:
                    _pq, _pg, _pp = screen.read_clocks()
                except Exception:
                    _pp = None
                # Madden freezes the play clock at its snap value and keeps it
                # on screen during the play, so a long jet sweep looks like a
                # play waiting at the line. Only snap a clock that is COUNTING:
                # a cross mid-play landed on the next playcall (Sep 29).
                if (_pp is not None and _pp <= C.PRESNAP_RESCUE_AT
                        and ever_played and "ACCEPT" not in txt):
                    time.sleep(1.1)
                    try:
                        _pp2 = screen.read_clocks()[2]
                    except Exception:
                        _pp2 = None
                    if _pp2 is None or _pp2 >= _pp:
                        log(f"[{stamp()}]   play clock :{_pp} is frozen "
                            f"(read :{_pp2}) - a live play, NOT snapping")
                        _pp = None
                if (_pp is not None and _pp <= C.PRESNAP_RESCUE_AT
                        and "PREPLAY" not in txt):
                    # ⛔ Oct 15 23:36: after a penalty prompt this fired on a
                    # PLAYCALL screen it could not read, and cross selected the
                    # X play (RPO READ SCREEN in the new playbook) and snapped
                    # it. Only snap when the line's own prompt is on screen.
                    _pp = None
                if (_pp is not None and _pp <= C.PRESNAP_RESCUE_AT
                        and ever_played and "ACCEPT" not in txt):
                    log(f"[{stamp()}]   at the line with a play selected and "
                        f"the clock at :{_pp} - SNAPPING to avoid a delay of game")
                    pad._remote_post("/focus", {})
                    time.sleep(0.25)
                    pad._remote_post("/press", {"buttons": ["cross"],
                                                "hold": C.OFF_SNAP_HOLD,
                                                "gap": 0.0, "focus": False})
                    idle_since = None
                    last_play_t = time.time()
                    continue

                if "ACCEPT" in txt and "DECLINE" in txt:
                    # A defensive penalty on our drive: a first down keeps the
                    # ball and the clock running.
                    log(f"[{stamp()}]   penalty prompt -> ACCEPT (cross)")
                    pad.press("cross", hold=0.06)
                    time.sleep(2.0)
                    idle_since = None
                    continue
                if any(k in txt for k in ("FINISH GAME", "RETURN TO HUB",
                                          "YOU WON", "YOU LOST",
                                          "POST GAME SUMMARY", "YOUR REWARDS",
                                          "SELECT GAME MODE", "ENTRY OPTIONS")):
                    log(f"[{stamp()}]   postgame detected -> menu handling")
                    idle_secs = 999
                    dead_t0 = time.time()

            if idle_secs >= 45:
                if dead_t0 is None:
                    log(f"[{stamp()}]   no playcall for 45s -> menu handling")
                    dead_t0 = time.time()
                got = advance_menus(log=log)
                menu_secs = time.time() - (dead_t0 or idle_since)
                dead_t0 = None
                # The game ended only if an end-of-game screen was seen (a long
                # cutscene is not a boundary). Book it even when the menus
                # stopped (level-end halt, lost menus): those rows were lost
                # twice on Oct 15.
                ended = SAW_FINISH_GAME and g.plays > 0
                if ended:
                    if FINAL_SCORE:
                        g.theirs, g.ours = FINAL_SCORE
                        g.opp_confirmed = True
                    g = _end_game(g, game_no, menu_secs, stamp, log)
                    game_no += 1
                    last_game_t = time.time()
                    game_stall_alerted = False
                    _clear_alert()
                    SAW_FINISH_GAME = False
                    FINAL_SCORE = None
                if got:
                    if not ended:
                        log(f"[{stamp()}]   break handled in {menu_secs:.0f}s")
                        g.need_chew = True
                        g.quarter = None
                    SAW_FINISH_GAME = False
                    FINAL_SCORE = None
                idle_since = None
                continue
            time.sleep(1.0)
            continue

        # -------------------------------------------------------------------
        # ON A PLAYCALL SCREEN
        # -------------------------------------------------------------------
        no_tab_streak = 0
        idle_since = None
        last_play_t = time.time()
        ever_played = True
        stuck_dumped = False

        log(f"[{stamp()}] [{total_plays + 1}] {side.upper():13s} "
            f"tab={name:<10s} ratio={ratio:.2f}")

        if QUIT_PENDING and CURRENT_EVENT and keys.level(CURRENT_EVENT) == 4:
            if not _l4_quit(log):
                return total_plays
            idle_since = None
            continue
        g.last_side = side
        g.nh_seen, g.nh_pressed = None, 0
        if side == "specialteams":
            snapped, status, hint = _special_teams(stamp, log)
        elif side == "offense":
            # The chew-clock tempo: first offensive play of Q1 and of Q3. Not
            # in overtime, where the clock running out on a tie is a loss.
            # ⭐ CHEW POLICY (Oct 16 08:45): Level 1 chews all game.
            # Level 2+ leaves chew OFF (NORMAL tempo, no play-clock wait) until
            # we are WINNING, then chews; a lost lead goes back to NORMAL.
            if not g.opp_confirmed:
                # No score yet (a fresh start or a fresh game): read it off
                # the scorebar before choosing, not after the snap.
                try:
                    h0 = screen.read_hud_full()
                except Exception:
                    h0 = None
                if h0 and h0.get("theirs") is not None \
                        and h0.get("ours") is not None:
                    if _accept_score(g, "ours", h0["ours"], log):
                        g.ours, g.since_ours = h0["ours"], 0
                    if _accept_score(g, "theirs", h0["theirs"], log):
                        g.theirs, g.since_theirs = h0["theirs"], 0
                        g.opp_confirmed = True
                    log(f"    pre-snap score {g.theirs}-{g.ours}")
            leading = g.opp_confirmed and g.ours > g.theirs
            if leading != LEADING:
                log(f"    OFFENCE -> "
                    f"{C.PLAYS[C.OFFENSE_LEADING if leading else trailing_key()]['name']}"
                    f" (score {g.theirs}-{g.ours}, "
                    f"{'leading' if leading else 'not leading'})")
            LEADING = leading
            lv = current_level() or 1
            chew_now = (not g.overtime) and C.CHEW_SET_TEMPO and (
                lv <= 1 or leading)
            want_tempo = "CHEW" if chew_now else (
                "NO HUDDLE" if (lv in C.NO_HUDDLE_LEVELS and not leading
                                and not g.overtime) else "NORMAL")
            if g.tempo != want_tempo:
                try:
                    _ps_now = screen.read_clocks()[2]
                except Exception:
                    _ps_now = None
                if _ps_now is not None and _ps_now < 20:
                    # A tempo change costs ~8s of play clock: not with this
                    # little left (a restart once arrived at :10 -> delay of
                    # game). Try again on the next offensive play.
                    log(f"    tempo -> {want_tempo} deferred (play clock "
                        f":{_ps_now})")
                else:
                    got = actions.set_tempo(want_tempo, log=log)
                    log(f"    tempo -> {want_tempo} "
                        f"{'SET' if got else 'FAILED'}  (LV{lv}, "
                        f"{'leading' if leading else 'not leading'})")
                    if got:
                        g.tempo = want_tempo
            # Snap floor follows the tempo actually set on the console.
            C.MIN_SNAP_WAIT = (C.SNAP_WAIT_NO_HUDDLE if g.tempo == "NO HUDDLE"
                               else C.SNAP_WAIT_NORMAL)
            play = current_offense()
            # Hybrid dive: short run while CONFIRMED ahead by FBD_SLOW_LEAD.
            if C.FBD_SLOW_LEAD and play.get("run_seq"):
                slow = (g.opp_confirmed
                        and (g.ours - g.theirs) >= C.FBD_SLOW_LEAD)
                if slow != g.fbd_slow:
                    g.fbd_slow = slow
                    log(f"    HYBRID DIVE: lead {g.ours - g.theirs:+d} -> "
                        + ("SHORT run (hold stops early)" if slow
                           else "FULL run"))
                if slow:
                    play = dict(play, run_seq=dict(C.FBD_SLOW_RUN))
            g.offense_used = play["name"]
            # No quarter-end skipping after four in a row, and none in OT.
            flip = (C.FLIP_PLAY and play.get("flip", False)
                    and off_plays % 2 == 1)
            snapped, status, hint = actions.call_offense(
                known=i, log=log,
                allow_skip=(g.skips < 4) and not g.overtime,
                # ⛔ Chew ALWAYS on (Oct 15 21:23: "we want chew
                # clock always on this"). Do not gate it on the score.
                chew=chew_now, play=play, flip=flip)
            if snapped:
                off_plays += 1
        else:
            _dlv = current_level() or 1
            _dov = C.DEF_TRAILING_BY_LEVEL.get(_dlv)
            if _dov and not LEADING:
                snapped, status, hint = actions.call_defense(
                    known=i, log=log, button=_dov[0], name=_dov[1])
            else:
                snapped, status, hint = actions.call_defense(known=i, log=log)
        log(f"    {status}")

        if not snapped:
            # A deliberate no-snap or a failed navigation: no play spent.
            # Oct 7: a failed tab navigation on a post-TD try was followed by a
            # delay of game and a kick, with no frame to show why - keep one.
            if "could not reach" in status:
                dump_stuck("tab-nav-failed", log)
            g.skips += 1
            time.sleep(max(1.0, hint))
            continue
        g.skips = 0

        # REPEATED-SITUATION STALL: plays are being called but nothing moves.
        # Reset below whenever the game clock changes (a long drive is not a
        # freeze).
        if (side, name) == last_situation:
            same_situation += 1
        else:
            same_situation = 0
        last_situation = (side, name)
        if same_situation == C.STALL_WARN:
            dump_stuck("repeated-situation", log)
            _alert(f"same situation {same_situation}x in a row "
                   f"({side}/{name}) - not progressing", log)
        if same_situation >= C.STALL_ABORT:
            dump_stuck("frozen-game", log)
            try:
                q, gs, _ps = screen.read_clocks()
                with open(C.FROZEN_FILE, "w") as fh:
                    json.dump({"situation": f"{side}/{name}",
                               "repeats": same_situation, "quarter": q,
                               "clock": gs, "score": f"{g.theirs}-{g.ours}",
                               "ts": time.strftime("%H:%M:%S")}, fh, indent=1)
            except Exception:
                pass
            _alert_sticky(
                f"FROZEN: {same_situation}x {side}/{name} with no progress - "
                f"STOPPING THE LOOP. This is the known Madden freeze (pause "
                f"works, play calling does not); quit the game by hand.", log)
            log(f"[{stamp()}] === ABORTING: game frozen ===")
            return total_plays

        total_plays += 1
        g.plays += 1
        g.event = g.event or CURRENT_EVENT
        g.since_ours += 1
        g.since_theirs += 1

        # -------------------------------------------------------------------
        # POST-SNAP READS, in the dead time while the play runs.
        # -------------------------------------------------------------------
        # The ARCADE/COMP badge only renders during a live play. Logged only.
        if g.badge is None and side == "offense":
            g.badge = screen.read_tier_badge()
            if g.badge:
                CURRENT_BADGE = g.badge
                _save_state()
                log(f"    badge: {g.badge}")

        # One HUD capture gives score, quarter, clocks and presentation.
        want_hud = ((g.quarter or 1) >= 3) or (g.plays % C.SCORE_EVERY == 0)
        h = screen.read_hud_full() if want_hud else None
        # From Q3, a stat ticker can cover the score right after the snap.
        tries = 0
        while (h is not None and h.get("theirs") is None
               and (g.quarter or 1) >= 3 and tries < C.SCORE_TICKER_RETRIES):
            tries += 1
            time.sleep(C.SCORE_TICKER_GAP)
            h2 = screen.read_hud_full()
            if h2 and h2.get("theirs") is not None:
                h = h2
        if h:
            if g.layout is None and h.get("clock_x") is not None:
                g.clock_x = h["clock_x"]
                g.layout = hud.layout_name(h["clock_x"])
                log(f"    presentation: {g.layout} (clock at x={g.clock_x})")
            if h["theirs"] is not None and h["ours"] is not None:
                if _accept_score(g, "ours", h["ours"], log):
                    g.ours, g.since_ours = h["ours"], 0
                if _accept_score(g, "theirs", h["theirs"], log):
                    g.theirs, g.since_theirs = h["theirs"], 0
                    g.opp_confirmed = True
                log(f"    score {g.theirs}-{g.ours} "
                    f"(lead {g.ours - g.theirs:+d})"
                    + ("" if g.opp_confirmed else "  [opp score NEVER read]"))

        if not g.overtime and g.seen_q4 and g.plays > 4:
            if screen.read_is_overtime():
                g.overtime = True
                log("    !! OVERTIME - sudden death, clock burning is off")

        q = h.get("quarter") if h else None
        if h and h.get("game") is not None:
            if last_clock is not None and h["game"] != last_clock:
                same_situation = 0
            last_clock = h["game"]

        if q is not None and g.first_quarter is None:
            g.first_quarter = q
            if q not in (1, C.START_QUARTER):
                g.partial = True
                log(f"    ! joined this game already in Q{q} - timing is NOT "
                    f"comparable, recording it as partial")
            elif g.joined and h and h.get("game") is not None \
                    and h["game"] >= C.FULL_GAME_CLOCK:
                # Joined, but at Q1 with a near-full clock: nothing was missed.
                g.partial = False
                g.joined = False
                log(f"    started from Q{q} with {h['game']}s on the clock - "
                    f"counting this as a FULL game")

        # Quarters only go 1 -> 2 -> 3 -> 4. Q4 back to Q1 with a full clock is
        # a new game (a backstop for a missed postgame); any other jump or step
        # back is a down misread as a quarter.
        _q1_clock = (h or {}).get("game")
        if (q in (1, C.START_QUARTER) and g.quarter == 4 and g.plays > 0
                and _q1_clock is not None and _q1_clock >= C.FULL_GAME_CLOCK):
            g = _end_game(g, game_no, 0, stamp, log, reason="quarter reset")
            game_no += 1
            last_game_t = time.time()
            game_stall_alerted = False
        elif (q and q != g.quarter and g.quarter is not None
                and q > g.quarter + 1):
            log(f"    ! IGNORED quarter Q{q} - cannot jump from "
                f"Q{g.quarter} (almost certainly a 4TH-down misread)")
        elif (q and g.quarter is not None and q < g.quarter
                and not (q == 1 and g.quarter in (3, 4))):
            log(f"    ! IGNORED quarter Q{q} - cannot go BACKWARDS from "
                f"Q{g.quarter} (almost certainly a down misread)")
        elif q and q != g.quarter:
            _gc = h.get("game") if h else None
            log(f"    quarter -> Q{q}"
                + (f"  (game clock {_gc}s"
                   + (" - FULL" if _gc >= C.FULL_GAME_CLOCK else "")
                   + ")" if _gc is not None else "  (clock unreadable)"))
            if q == 4:
                g.seen_q4 = True
            if q == 3 and g.quarter is not None and g.quarter < 3:
                # Halftime resets the tempo; set it again on the next offence.
                log("    Q3 -> halftime reset the tempo to NORMAL")
                g.need_chew = True
                g.tempo = "NORMAL"
            g.quarter = q

        # Poll fast during a live play (a cheap pixel scan) and right after one.
        _cheap = (name == "NO-TABSTRIP") or (last_play_t >= _t_iter)
        time.sleep(C.LOOP_SLEEP_LIVE if _cheap else C.LOOP_SLEEP)
    return total_plays


def _end_game(g, game_no, menu_secs, stamp, log, reason="postgame"):
    """Log, alert and record a finished game. Returns a fresh Game."""
    global LAST_ROW_TS, RECORD_AT_LAST_GAME
    secs = time.time() - g.t0
    log(f"[{stamp()}] === GAME {game_no} END ({reason}): "
        f"{g.plays} plays, {secs/60:.1f} min"
        + (f" | between-games {menu_secs:.0f}s" if menu_secs else "") + " ===")
    log(f"    final {g.theirs}-{g.ours} -> {g.result}"
        + ("" if g.opp_confirmed else
           "   ! opp score NEVER confirmed - result unreliable"))
    if g.result == "L":
        # From the score reader, which misreads. The progress screen's
        # "x/1 LOSSES" is the truth: check it.
        # Not sticky: the score reader misreads (stat ticker) and the progress
        # screen is the truth - a real run loss raises its own sticky alert.
        _alert(f"GAME {game_no} LOST {g.theirs}-{g.ours} by the score "
                      f"reader - check the progress screen / event record", log)
    elif g.result is None:
        log("    ! RESULT UNKNOWN from the score - leaving it for the record "
            "change to decide")
    elif g.result == "T":
        log("    ! TIE - recorded as a tie, not assumed to be a win")
    LAST_ROW_TS = _record_game(
        record=CURRENT_RECORD, badge=g.badge,
        hud_layout=g.layout, hud_clock_x=g.clock_x,
        partial=g.partial, first_quarter=g.first_quarter,
        plays=g.plays, min=round(secs / 60, 1), gap=int(menu_secs),
        theirs=g.theirs, ours=g.ours, result=g.result,
        opp_confirmed=g.opp_confirmed, overtime=g.overtime,
        hike_at=C.HIKE_AT, snap_wait=C.MIN_SNAP_WAIT,
        offense=g.offense_used,
        # ⛔ The row is written after the menus - by then a level-complete walk
        # has already moved CURRENT_EVENT on (L3 game 3 was booked as #1988).
        event=g.event or CURRENT_EVENT,
        level=keys.level(g.event) if g.event else current_level())
    RECORD_AT_LAST_GAME = CURRENT_RECORD
    _save_state()
    return Game(joined=False)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _require_calibration(log=print):
    """Refuse to farm on crop regions nobody has checked on this setup."""
    if os.environ.get("MUT_EVENT_FORCE") == "1":
        log("  ! MUT_EVENT_FORCE=1 - running on UNVERIFIED crop regions")
        return True
    if os.path.exists(C.CALIB_FILE):
        try:
            with open(C.CALIB_FILE) as fh:
                d = json.load(fh)
            log(f"  calibration: verified {d.get('ts', '?')}")
            return True
        except (OSError, ValueError):
            pass
    print("REFUSING TO START: the crop regions have not been verified.\n"
          "  Bring the stream up, get to a playcall screen, and run:\n"
          "      ./calibrate.py verify\n"
          "  Override (not recommended) with MUT_EVENT_FORCE=1.")
    return False


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "loop"
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    if cmd == "loop":
        if not _require_calibration():
            sys.exit(2)
        # Start from a known-neutral pad: a previous crash may have left keys down.
        try:
            pad.release()
        except Exception:
            pass
        play_loop(max_plays=int(arg) if arg else 99999,
                  log=functools.partial(print, flush=True))
    elif cmd == "menus":
        print(advance_menus())
    elif cmd == "event":
        print(enter_event())
    elif cmd == "lineup":
        print(actions.choose_lineup())
    elif cmd == "chew":
        print("chew clock:", actions.set_chew_clock())
    elif cmd == "off":
        print(actions.call_offense())
    elif cmd == "def":
        print(actions.call_defense())
    else:
        print(__doc__)
