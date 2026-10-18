#!/usr/bin/env python3
"""Every event-specific setting, in one place.

When the event changes, this is the only file that should need editing. Every
value can be overridden with the environment variable named next to it.

Screen regions are (x, y, w, h) in display points on a 1920x1080 chiaki-ng
stream with no letterboxing. `calibrate.py verify` checks them against a live
playcall screen and writes the stamp file `grind.py` requires before it runs.
"""
import os


def _env(name, default):
    return os.environ.get(name, default)


# ---------------------------------------------------------------------------
# THE EVENT: MOST FEARED - TRICK-OR-TREAT CPU EVENTS (Oct 15 - Nov 5 2026)
# ---------------------------------------------------------------------------
# PLAY tab -> tile "MF: TRICK OR TREAT CPU EVENTS" -> a list of 10 events in 4
# levels (map: ../MOST FEARED MAP.md). Each event costs its own numbered KEY.
# L1 keys are bought; finishing a level (3 wins) pays a random next-level key.
# The farm plays the highest level it holds a key for (tools/keys.py ledger).
# All: vs CPU, 11on11, Active MUT Team, No Kicks/Punts, points can be STOLEN.
EVENT_LABEL = _env("MUT_EVENT_LABEL", "MF: Trick-or-Treat")
# The hub tile and the words that prove we are in the MF list, not EVENTS.
HUB_TILE_WORDS = ("TRICK OR TREAT", "PLAY MF CPU")
MF_LIST_WORDS = ("MURKWATER", "CATACOMB", "PHANTASMAL", "ABYSSAL")
# ⭐ STOP AT LEVEL END (Oct 15): when a level's last win shows (3/3)
# or FINISH EVENT appears, press NOTHING, screenshot, halt. The reward/key
# path (instant? a pack? on FINISH EVENT?) is unknown until walked by hand.
# Off from Oct 15 22:10: the level-end walk is automated (grind._mf_rewards):
# Finish Event -> open the "Most Feared Level N CPU Event Key" pack -> read the
# key -> circle. L4: advance through everything, open nothing.
PAUSE_AT_LEVEL_END = _env("MUT_EVENT_PAUSE_LEVEL_END", "0") == "1"
# Wins that complete a level (progress screen "x/3 WINS").
LEVEL_WINS = int(_env("MUT_EVENT_LEVEL_WINS", "3"))
# Unused by this event (cards are matched by key number in grind.enter_event).
EVENT_MATCH = _env("MUT_EVENT_MATCH", r"MF")
EVENT_REQUIRE = ()
EVENT_REJECT = ()
EVENT_TITLE_WINDOW = 60
# Games START IN Q3 (Oct 15). A game first seen in this quarter with
# a near-full clock is a full game, not a partial one.
START_QUARTER = int(_env("MUT_EVENT_START_QUARTER", "3"))

QUARTER_MINS = int(_env("MUT_EVENT_QUARTER_MINS", "3"))
# A Q1 clock at or above this means the loop saw the game from kickoff.
FULL_GAME_CLOCK = int(_env("MUT_EVENT_FULL_CLOCK", str(QUARTER_MINS * 60 - 15)))

# Off: a loss (or a completed 10/10) rolls straight into a fresh run, which is
# what you want when running unattended. On (=1): stop before entering another
# game after any loss and leave a HALT file that farm.sh refuses to start over.
HALT_ON_ANY_LOSS = _env("MUT_EVENT_HALT_ANY_LOSS", "0") != "0"

# ---------------------------------------------------------------------------
# MENUS
# ---------------------------------------------------------------------------
# Rows are found by their words and the focused one by brightness, never by
# position. Reject words are checked first ("RESTRICTED" is inside
# "UNRESTRICTED").
# RESTRICTED (Oct 1 19:20: lineup fixed, "the lineup restricted
# version is available. I just confirmed"). It needs at least 3x 85+ OVR BO
# players; the farm HALTS on the LOCKED popup if that ever stops being true.
# Only read on the entry-options screen, i.e. when a NEW run starts.
LINEUP_WANT = _env("MUT_EVENT_LINEUP", "RESTRICTED").upper()
LINEUP_ROWS = {
    "RESTRICTED": {"want": ("RESTRICTED",), "not": ("UNRESTRICTED",)},
    "FREE": {"want": ("FREE", "UNRESTRICTED"), "not": ()},
}
POSTGAME_ROWS = {
    "FINISH": {"want": ("FINISH GAME",), "not": ()},
    "OTHER": {"want": ("VIEW HIGHLIGHTS", "PLAYER STATS", "TEAM STATS",
                       "SCORE SUMMARY"), "not": ()},
}
POSTGAME_WANT = "FINISH"

# ---------------------------------------------------------------------------
# THE PLAYS
# ---------------------------------------------------------------------------
# Button names are the pad daemon's: cross, moon (circle), pyramid (triangle),
# box (square). There is no "triangle" key.
#
# OFFENCE: JET SWEEP (Oct 1, the user), on square in the offensive FAVORITES.
# Select it, wait SETTLE seconds so the selection lands, snap, and let the CPU
# run it: no stick, no R2. FLIPPED every other offensive play (R2 alone, before
# the select), skipped when the play clock is at FLIP_MIN_CLOCK or under.
# ⛔ The settle exists because an urgent snap ~0.5s after square landed on the
# playcall screen and cross picked the X play instead (Sep 29).
#
# The hybrid FB DIVE is kept one switch away: MUT_EVENT_OFFENSE=dive. It is
# selected, snapped, then the left stick is held UP with R2 (sprint) from
# `after` seconds for up to `hold` seconds; the daemon lets go the moment the
# playcall screen comes back.
#
# Keep a play you are happy to run on cross (X) as well: cross is also the
# snap button, so a stray snap on the playcall screen selects whatever is there.
# Do not put the play on circle: circle is the loop's "back out" key.
OFF_TAB = _env("MUT_EVENT_OFF_TAB", "FAVORITES")
PLAYS = {
    "jet": {
        "name": "JET SWEEP",
        "button": _env("MUT_EVENT_JET_BUTTON", "box"),
        "run_seq": None,
        "flip": True,
        "settle": float(_env("MUT_EVENT_JET_SETTLE", "1.0")),
    },
    # ⭐ F PUSH SWING (Gun Ace Wk), Oct 15 23:20, the user's new playbook,
    # labbed in practice vs a defence: snap -> R1 BULLET at +2.3s held 0.55s
    # (+2.0 never registered; +3.0 was "a second late"; 2.3 -> 2.15 Oct 15 23:45,
    # "throw just a few milliseconds earlier") -> stick UP
    # + R2 from +3.8s for up to 3s, released when the playcall screen returns.
    "swing": {
        "name": "F PUSH SWING",
        "button": _env("MUT_EVENT_SWING_BUTTON", "pyramid"),
        "run_seq": {"throw": {"after": float(_env("MUT_EVENT_SWING_THROW_AT", "2.15")),
                              "hold": float(_env("MUT_EVENT_SWING_THROW_HOLD", "0.55")),
                              "button": "r1"},
                    "after": float(_env("MUT_EVENT_SWING_RUN_AFTER", "3.65")),
                    "hold": float(_env("MUT_EVENT_SWING_RUN_HOLD", "3.0"))},
        # Flipped every other offensive play (Oct 15 23:35). The
        # receiver stays on R1 when flipped (Sep 13, same play).
        "flip": True,
        "settle": 0.0,
    },
    # ⭐ INSIDE ZONE (Gun Doubles Clamp Stack), X in the old playbook (Oct 16).
    # The Clock's Ticking setup that won tier 4 COMP twice (Sep 19, W 14-0 and
    # W 8-0, the only fully automated COMP wins): snap, then L-stick UP + R2
    # from +1.0s for 3.0s (a 2.0s start was too late). No throw, no flip.
    # ⛔ X is also the snap button: select (X) -> 6s floor -> snap (X).
    "iz": {
        "name": "INSIDE ZONE",
        "button": _env("MUT_EVENT_IZ_BUTTON", "cross"),
        "run_seq": {"after": float(_env("MUT_EVENT_IZ_RUN_AFTER", "1.0")),  # 3.0 test (Oct 16 13:10) went 3-2 on L3, both losses shutouts -> back to 1.0 (6-0) at 14:25
                    "hold": float(_env("MUT_EVENT_IZ_RUN_HOLD", "3.0"))},
        "flip": False,
        "settle": 0.0,
    },
    # FB DIVE WEAK: TRIANGLE in the old playbook (back in use Oct 15 23:58);
    # it was SQUARE in the F Push Swing playbook - MUT_EVENT_FBD_BUTTON=box.
    "dive": {
        "name": "FB DIVE WEAK",
        "button": _env("MUT_EVENT_FBD_BUTTON", "pyramid"),
        "run_seq": {"after": float(_env("MUT_EVENT_FBD_RUN_AFTER", "1.0")),
                    "hold": float(_env("MUT_EVENT_FBD_RUN_HOLD", "3.0"))},
        "flip": False,
        "settle": 0.0,
    },
}
OFFENSE = PLAYS[_env("MUT_EVENT_OFFENSE", "dive")]
# ⭐ Per level (Oct 15): FB DIVE on L1 and L2, JET SWEEP on L3 and L4.
# ⭐ Oct 15 21:27 "jet sweep till we are winning...then we switch to
# fb dive". Every game starts DOWN 0-5 in Q3. Leading = opponent score
# confirmed and ours strictly higher; a tie or a deficit goes back to jet.
# Oct 15 23:58 (user): back to the OLD playbook - JET SWEEP (square) until
# winning, then FB DIVE (triangle). The F Push Swing playbook is one switch
# away: MUT_EVENT_OFF_TRAILING=swing MUT_EVENT_FBD_BUTTON=box.
OFFENSE_TRAILING = _env("MUT_EVENT_OFF_TRAILING", "jet")
OFFENSE_LEADING = _env("MUT_EVENT_OFF_LEADING", "dive")
# ⭐ Oct 16 09:10 (user): test INSIDE ZONE on Level 2+ while not winning
# (both overnight L2 losses were jet-sweep shutouts). Level 1 stays on jet.
OFFENSE_TRAILING_HI = _env("MUT_EVENT_OFF_TRAILING_HI", "iz")
# Oct 16 10:40 (user): INSIDE ZONE on EVERY level ("might make games faster").
OFFENSE_HI_FROM_LEVEL = int(_env("MUT_EVENT_OFF_HI_FROM", "1"))
# Per-level override of the not-winning play (wins over the two settings
# above). Oct 16 11:55 (user): JET SWEEP on L4 to test something.
# Oct 16 16:50 (user): L4 uses the SAME settings as L3 (inside zone).
# The Jet/No-Huddle L4 test is one env switch away: MUT_EVENT_OFF_TRAILING_L4=jet
OFFENSE_TRAILING_BY_LEVEL = ({4: _env("MUT_EVENT_OFF_TRAILING_L4", "")}
                             if _env("MUT_EVENT_OFF_TRAILING_L4", "") else {})
# ⭐ L4 test (Oct 16 12:40): while NOT winning on these levels the
# tempo is NO HUDDLE (no playcall screen between plays - the loop snaps at the
# line itself), and the DEFENCE calls the play on X instead of MID BLITZ.
# Winning -> FB DIVE + CHEW CLOCK + MID BLITZ as everywhere else.
NO_HUDDLE_LEVELS = set()   # was {4} (Oct 16 L4 test) - L4 now plays like L3
# ⭐ L4 "WIN ONE AND QUIT" (Oct 16): any L4 run end pays candy + an
# L3 key, so after ONE L4 win the farm starts game 2 and QUITS it at once.
L4_QUIT_AFTER_WINS = int(_env("MUT_EVENT_L4_QUIT_AFTER", "1"))
# First time: press QUIT GAME, then HALT at the confirm popup for a look.
L4_QUIT_SUPERVISED = _env("MUT_EVENT_L4_QUIT_SUPERVISED", "0") == "1"  # off: the user away (Oct 16 17:00)
# Oct 16 13:05 (user): L4 defence back to MID BLITZ - override removed.
DEF_TRAILING_BY_LEVEL = {}
# No Huddle at the line: first snap attempt this long after PREPLAY appears,
# then one X tap per NH_SNAP_EVERY until the play starts.
NH_SNAP_FIRST = float(_env("MUT_EVENT_NH_FIRST", "1.5"))
# The playcall screen DOES still appear under No Huddle (Oct 16):
# only the snap floor changes - players take longer to get set. 10s, not 6.
SNAP_WAIT_NO_HUDDLE = float(_env("MUT_EVENT_SNAP_WAIT_NH", "10.0"))
# ⛔ OFF (Oct 16: "no extra presses just timing"). The at-the-line
# X taps in grind.play_loop are a fallback for a No Huddle with no playcall
# screen; it does not happen, and stray X presses can wreck a live play.
NH_LINE_TAPS = _env("MUT_EVENT_NH_LINE_TAPS", "0") == "1"
# ⛔ PREPLAY stays up through the 5-6s jet motion: a second tap any sooner
# would snap the ball mid-motion. Only re-tap once the motion must be over.
NH_SNAP_EVERY = float(_env("MUT_EVENT_NH_EVERY", "10.0"))
FLIP_PLAY = _env("MUT_EVENT_FLIP", "1") == "1"
FLIP_MIN_CLOCK = int(_env("MUT_EVENT_FLIP_MIN_CLOCK", "9"))
# Hybrid dive only: once CONFIRMED ahead by this many points, switch to a short
# hold so the back stops early. 0 = always the full run.
FBD_SLOW_LEAD = int(_env("MUT_EVENT_FBD_SLOW_LEAD", "10"))
FBD_SLOW_RUN = {"after": float(_env("MUT_EVENT_FBD_SLOW_AFTER", "0.2")),
                "hold": float(_env("MUT_EVENT_FBD_SLOW_HOLD", "1.5"))}

# DEFENCE: MID BLITZ on square in the defensive FAVORITES, called with
# the user's MACRO (Oct 1): hold square, tap D-pad DOWN three times inside the
# hold, release. Selecting it this way applies his custom adjustments.
# ⛔ One daemon /combo call - the taps must land INSIDE the hold. The timings
# below are the ones proven live (Sep 14 all day, Sep 25): change only the
# button or the tap count, never pre/tap_hold/gap/post on a hunch.
# MUT_EVENT_DEF_MACRO=0 = a plain press of square.
DEF_TAB = _env("MUT_EVENT_DEF_TAB", "FAVORITES")
# ⭐ OLB FIRE MAN on X (Oct 2 20:45: "try a new defense... mapped
# to X in favorites its called OLB Fire Man"). Plain press. Was MID BLITZ on
# square (plain avg 12.0 min over 45 games; macro 12.6 over 50).
# Restore: MUT_EVENT_DEF_BUTTON=box MUT_EVENT_DEF_NAME="MID BLITZ".
# ⛔ OLB FIRE MAN REVERTED after 1 game (Oct 2 21:42, "lets move back
# to mid blitz that is terrible" - 18.2 min, 41 plays). Back to MID BLITZ, square.
DEF_PLAY_BUTTON = _env("MUT_EVENT_DEF_BUTTON", "box")
# ⭐ MACRO OFF - TEST (Oct 2 09:45: "lets test defense without the
# macro on mid blitz for awhile just vanilla mid blitz"). Plain press of square.
# Baseline with the macro (chew off, Restricted): 50 games, 12.6 min avg.
# To restore: MUT_EVENT_DEF_MACRO=1.
DEF_MACRO = _env("MUT_EVENT_DEF_MACRO", "0") == "1"
DEF_MACRO_PRE = float(_env("MUT_EVENT_DEF_MACRO_PRE", "1.0"))
DEF_MACRO_TAPS = _env("MUT_EVENT_DEF_MACRO_TAPS", "down down down").split()
DEF_MACRO_TAP_HOLD = float(_env("MUT_EVENT_DEF_MACRO_TAP_HOLD", "0.08"))
DEF_MACRO_GAP = float(_env("MUT_EVENT_DEF_MACRO_GAP", "0.14"))
DEF_MACRO_POST = float(_env("MUT_EVENT_DEF_MACRO_POST", "0.0"))
DEF_PLAY_NAME = _env("MUT_EVENT_DEF_NAME", "MID BLITZ")

# Never punt and never kick a field goal: on a 4th-down special-teams screen,
# go back to FAVORITES and run the play. (Field goals are not in this event's
# scoring rules.) Kickoffs use the same screen and are left alone.
GO_FOR_IT_ON_FOURTH = _env("MUT_EVENT_GO_FOR_IT", "1") == "1"

# Press lengths. 200 ms: shorter presses were sometimes dropped over Remote Play.
OFF_SELECT_HOLD = float(_env("MUT_EVENT_SELECT_HOLD", "0.20"))
OFF_SNAP_HOLD = float(_env("MUT_EVENT_SNAP_HOLD", "0.20"))
# The run keys.
OFF_RUN_STICK = _env("MUT_EVENT_RUN_STICK", "lstick_up")
OFF_RUN_SPRINT = _env("MUT_EVENT_RUN_SPRINT", "r2")

# Before pressing the play button, check the visible favourites row shows the
# play (12 favourites = 4 rows, and stray stick input scrolls them). If another
# row shows, scroll and re-check; if the names are unreadable, press anyway.
PLAY_ROW = (70, 835, 1790, 70)          # the three play-name cards
PLAY_ROW_CHECK = _env("MUT_EVENT_PLAY_ROW_CHECK", "1") == "1"
PLAY_ROW_SCROLL_TRIES = int(_env("MUT_EVENT_PLAY_ROW_TRIES", "4"))

# What the daemon watches to end the run hold: the play-card band goes dark
# (>= PLAY_BAND_DARK) AND the L1 badge on the tab strip lights up. Both are only
# true on the playcall page.
PLAY_BAND = (80, 570, 1760, 250)
PLAY_BAND_DARK = float(_env("MUT_EVENT_PLAY_BAND_DARK", "0.70"))
PLAY_L1_BADGE = (72, 522, 44, 30)
PLAY_L1_BRIGHT = float(_env("MUT_EVENT_PLAY_L1_BRIGHT", "0.25"))

# ---------------------------------------------------------------------------
# THE CLOCK
# ---------------------------------------------------------------------------
# Two separate things:
#   1. The CHEW CLOCK tempo (R3 -> TEMPO), set on the first offensive play of
#      Q1 and again in Q3, because halftime resets it. On all game.
#   2. Waiting at the line for the play clock to fall to HIKE_AT before the
#      snap, but only while the game clock is actually running.
# ⭐ CHEW CLOCK OFF (Oct 1 21:55): with the 28-point mercy rule we
# are no longer racing the 4th-quarter clock, and chewing was running out the
# 2nd quarter - the CPU receives the Q3 kickoff, so a drive lost before half is
# a lost possession. Snap on the MIN_SNAP_WAIT floor instead. Watch game times.
# To restore: MUT_EVENT_SET_TEMPO=1 MUT_EVENT_HIKE_AT=14.
# ⭐ CHEW CLOCK ON (Oct 15, Most Feared): games start in Q3, so the
# tempo is set once, on the first offensive play. Chew 22/14 as UNSTOPPABLE.
CHEW_SET_TEMPO = _env("MUT_EVENT_SET_TEMPO", "1") == "1"

# Wait at least this long after calling a play before snapping. The game will
# not take a snap until the players are set; 3.0s was tested and snaps were
# refused. 6.0 is the known-good value.
MIN_SNAP_WAIT = float(_env("MUT_EVENT_SNAP_WAIT", "6.0"))
SNAP_WAIT_NORMAL = MIN_SNAP_WAIT
# Arriving with this much play clock or less: wait the floor and snap.
CHEW_SKIP_BELOW = int(_env("MUT_EVENT_CHEW_SKIP_BELOW", "22"))
# Otherwise snap when the play clock reaches this. 99 = never wait.
HIKE_AT = int(_env("MUT_EVENT_HIKE_AT", "14"))
# Furthest out (seconds) a snap is scheduled from a single play-clock reading.
SNAP_SCHEDULE_MAX = float(_env("MUT_EVENT_SNAP_SCHED_MAX", "12"))
# Hard ceiling on the whole pre-snap wait.
SNAP_DEADLINE = 42.0

# Seen at the line with a play selected and this much play clock or less, but
# not on the playcall screen: the play was selected without a snap. Snap it.
PRESNAP_RESCUE_AT = int(_env("MUT_EVENT_PRESNAP_RESCUE", "22"))

# ---------------------------------------------------------------------------
# SCREEN REGIONS
# ---------------------------------------------------------------------------
TABSTRIP = (75, 515, 1160, 45)     # playcall tab strip, both sides
CLOCKS = (1180, 1005, 780, 80)     # "2nd | 1:07 | :26 | 4TH & 6"
SCORE_L = (520, 1006, 250, 76)     # opponent code + score
SCORE_R = (770, 1006, 270, 76)     # our score + code
ACTIONBAR = (380, 915, 1180, 48)   # "ADD/REMOVE FAVORITE ... BACK"
TEMPO_LABEL = (90, 585, 300, 48)   # the words "TEMPO ADJUSTMENT"
TEMPO_VALUE = (351, 580, 300, 60)  # the < NORMAL > box next to it
# Score crops must include the team code: Vision returns nothing for a lone digit.

# Tab layouts. The active tab is a light box behind dark text, so "which tab"
# is a brightness question. Centres are x offsets from TABSTRIP's left edge.
OFF_TABS = ["COACH", "FORMATION", "CONCEPT", "PLAY TYPE", "PLAYER",
            "PERSONNEL", "FAVORITES", "RECENT"]
OFF_X = [149, 316, 436, 549, 654, 794, 949, 1056]
DEF_TABS = ["COACH", "PERSONNEL", "CONCEPT", "PLAY TYPE", "FAVORITES", "RECENT"]
DEF_X = [149, 313, 436, 548, 666, 772]
ST_TABS = ["NORMAL", "HEAVY", "PUNT"]
ST_X = [96, 189, 268]

# Which side we are on is decided by how far right the strip's text reaches.
EXTENT_OFFENSE = 980
EXTENT_DEFENSE = 600

# Minimum peak/median brightness ratio to act on a tab read. Good reads land
# 2.9-3.5; misreads came from 1.8-2.0.
CONFIDENT = 2.4

# Words that only appear in a playcall tab strip.
TAB_WORDS = ("COACH", "SUGGESTIONS", "FORMATION", "CONCEPT", "PLAY TYPE",
             "PLAYER", "PERSONNEL", "GROUP", "FAVORITES", "RECENT",
             "NORMAL", "HEAVY", "PUNT")

# ---------------------------------------------------------------------------
# PACING
# ---------------------------------------------------------------------------
LOOP_SLEEP = float(_env("MUT_EVENT_LOOP_SLEEP", "1.0"))
# Poll interval during a live play (a cheap pixel scan) and right after a play.
LOOP_SLEEP_LIVE = float(_env("MUT_EVENT_LOOP_SLEEP_LIVE", "0.3"))
# Full-screen OCR while off the playcall screen, at most this often (seconds).
SLOW_PATH_EVERY = float(_env("MUT_EVENT_SLOW_PATH_EVERY", "4.0"))
# Check for a latched "hold to select" overlay on every Nth play call.
OVERLAY_CHECK_EVERY = int(_env("MUT_EVENT_OVERLAY_CHECK_EVERY", "5"))

# ---------------------------------------------------------------------------
# SCORE READING
# ---------------------------------------------------------------------------
# The score is read after the snap, during the play: every SCORE_EVERY plays
# in the first half and every play from Q3. It only feeds the hybrid-dive lead
# check and the history; wins and losses come from the event's own screens.
SCORE_EVERY = int(_env("MUT_EVENT_SCORE_EVERY", "2"))
# From Q3, a stat ticker can cover the score right after the snap: retry.
SCORE_TICKER_RETRIES = int(_env("MUT_EVENT_SCORE_RETRIES", "1"))
SCORE_TICKER_GAP = float(_env("MUT_EVENT_SCORE_RETRY_GAP", "1.0"))
# Scores CAN go down in this event (points are stolen), so a lower reading is
# accepted once it repeats this many times in a row.
SCORE_CONTRADICT = int(_env("MUT_EVENT_SCORE_CONTRADICT", "2"))
# Points are stolen one at a time here, so 1 is a legal score.
ALLOW_SCORE_OF_ONE = _env("MUT_EVENT_ALLOW_ONE", "1") == "1"
# A jump bigger than this per unread play (and never more than SCORE_JUMP_ABS in
# one read) is a misread until it repeats 3x.
SCORE_JUMP_MAX = 8
SCORE_JUMP_ABS = int(_env("MUT_EVENT_SCORE_JUMP_ABS", "16"))
# Nothing above this is a score (stat and field-position numbers share the strip).
SCORE_MAX = int(_env("MUT_EVENT_SCORE_MAX", "99"))

# ---------------------------------------------------------------------------
# WATCHDOGS
# ---------------------------------------------------------------------------
# The same side/tab this many times in a row with the game clock not moving:
# warn, then stop (the known Madden freeze: pause works, play calling does not).
STALL_WARN = int(_env("MUT_EVENT_STALL_WARN", "16"))
STALL_ABORT = int(_env("MUT_EVENT_STALL_ABORT", "40"))
# No game finished in this long: alert.
GAME_STALL_SECS = int(_env("MUT_EVENT_GAME_STALL", "2700"))

# A frozen chiaki feed reads as a valid screen forever, so byte-identical
# captures are counted. Stop trusting the picture after STALE_FRAMES; restart
# the stream after FROZEN_RESTART, but only if a playcall screen was seen in the
# last FROZEN_ONLY_WITHIN seconds (menus are legitimately static) and the
# freeze survives a FROZEN_CONFIRM_SECS re-check (a restart costs 25-220s).
STALE_FRAMES = int(_env("MUT_EVENT_STALE_FRAMES", "3"))
FROZEN_RESTART = 6
FROZEN_ALERT = 14
FROZEN_ONLY_WITHIN = float(_env("MUT_EVENT_FROZEN_WITHIN", "120"))
FROZEN_CONFIRM_SECS = float(_env("MUT_EVENT_FROZEN_CONFIRM", "4.0"))

PS5_HOST = _env("MUT_PS5_HOST", "192.168.1.100")

# ---------------------------------------------------------------------------
# FILES
# ---------------------------------------------------------------------------
RUN_DIR = "/tmp/mut-event"
SHOT_DIR = RUN_DIR
STUCK_DIR = f"{RUN_DIR}/stuck"
# One history file per event, so averages never mix events.
HISTORY = os.path.expanduser(_env("MUT_EVENT_HISTORY",
                                  "~/.mut_most_feared_history.jsonl"))
# One row per run event: started / COMPLETE / LOST / key earned.
RUNS = os.path.expanduser(_env("MUT_EVENT_RUNS",
                               "~/.mut_most_feared_runs.jsonl"))
STATE_FILE = os.path.expanduser("~/.mut_event_state.json")
CALIB_FILE = os.path.expanduser("~/.mut_event_calibration.json")

ALERT_FILE = f"{RUN_DIR}/ALERT"
STICKY_ALERT = f"{RUN_DIR}/ALERT.STICKY"   # only a human clears this
HALT_FILE = f"{RUN_DIR}/HALT"              # farm.sh will not start over it
FROZEN_FILE = f"{RUN_DIR}/FROZEN"


def _validate_buttons():
    """Refuse to start on a button name the pad daemon does not know.

    An unknown name is rejected by the daemon with nothing pressed, which looks
    exactly like a timing problem. Fail loudly here instead.
    """
    try:
        import pad
        keys = set(getattr(pad, "KEYS", {}) or {})
    except Exception:
        return
    if not keys:
        return
    named = {"DEF_PLAY_BUTTON": DEF_PLAY_BUTTON,
             "OFFENSE button": OFFENSE["button"],
             "OFF_RUN_STICK": OFF_RUN_STICK,
             "OFF_RUN_SPRINT": OFF_RUN_SPRINT}
    bad = {k: v for k, v in named.items() if v and v not in keys}
    if bad:
        raise SystemExit(
            "\nCONFIG ERROR - these buttons do not exist in the pad key map:\n"
            + "\n".join(f"    {k} = {v!r}" for k, v in bad.items())
            + f"\n\n  valid keys: {', '.join(sorted(keys))}\n"
              "  (triangle is 'pyramid', circle is 'moon', square is 'box')\n")


_validate_buttons()
