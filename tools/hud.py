#!/usr/bin/env python3
"""Read the live HUD by CONTENT, not by fixed coordinates.

⛔⛔ WHY THIS EXISTS. MADDEN USES MORE THAN ONE SCOREBOARD LAYOUT, and they
put the same values in different places:

    game 1   wide bar across the bottom; clock at x~1180-1530; both scores
             left of it, cycling with stat pop-ups
    game 2   compact CENTRED block; clock at x~860-1055; one score each side,
             and the down-and-distance on a SECOND ROW above the clock

A fixed crop cut for the first layout is pinned to it, so in
the second one CLOCKS sampled BARE GRASS. The farm lost the game clock, the play
clock, the quarter and the score at once: 7 of 7 snaps fell through to "clock
unreadable", silently disabling clock chewing, quarter tracking and the Q4
bail-out. Nothing raised its hand, because every one of those failures is a
legal "not readable right now".

⛔ A crop set per layout is NOT the fix - Madden ships several broadcast
packages and we cannot enumerate them. Find values by WHAT THEY LOOK LIKE and
where they sit RELATIVE TO EACH OTHER, and the layout stops mattering.

⭐ TWO THINGS THIS FILE GETS RIGHT THAT A FLAT TOKEN LIST CANNOT:

  * ROWS. The centred block stacks "1ST & GOAL" ABOVE "2ND 1:17 :34", and their
    x-ranges OVERLAP. Sorting tokens by x alone interleaves the two rows and
    makes the down look like it follows the quarter. Group by y first.

  * MULTI-SCALE VOTING. Measured on one frame: the wide crop found the
    opponent's score and read OURS as "U"; the tight crop found ours and lost
    the opponent's. The failures are NOT correlated, which is exactly why the
    archive votes across upscales instead of trusting one.
"""
import os
import re
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import Quartz  # noqa: E402
from Foundation import NSURL  # noqa: E402

import config as C  # noqa: E402
import pad  # noqa: E402
import screen  # noqa: E402

# Wide enough for every layout seen (game 1 clock reaches x~1530, game 2's left
# team code sits at x~660), shallow enough to exclude the field and stay cheap.
HUD_BAND = (0, 950, 1920, 130)

# ⛔⛔ SUB-CROPS, NOT JUST SCALES. Measured on one frame of the centred layout:
#     wide crop   found the opponent's "PHI 0", MISSED our score entirely,
#                 and read "2ND" as "200"
#     tight crop  found our "0" beside our code, MISSED the opponent's
# Neither crop alone carries all four score tokens. Vision's failures are not
# correlated across crops any more than across scales, so the answer is the
# same: read several, merge, and let the regexes filter the garbage.
#
# Offsets are BAND-RELATIVE and are added back, so every box from every pass
# lands in one coordinate space and duplicate readings group into the same row.
HUD_SUBCROPS = ((0, 0, 1920, 130), (380, 0, 1250, 130),
                (480, 10, 960, 120))
HUD_SCALES = (2, 3)

ROW_TOL = 14          # px: how close two boxes must be in y to be one row
# Minimum x separation between the two scores. Copies of the SAME numeral
# land within a few px of each other; the real pair straddles the bar.
SCORE_MIN_GAP = 120
# ⛔⛔ The REAL pair sits only ~115px
# apart here: "CIN@558 | 5@693 | 5@708 | 9@808 | JOH@939" -> 808-693 = 115,
# refused by the 120 above. That is why this event logged 0-13 score reads a
# game, and why the 2nd-half pass switch could not see the score. A duplicate
# numeral sits ~15px from its twin, so 120 is only needed where the two values
# are EQUAL (the phantom tie it was written for). Different values need only
# clear the duplicate spacing.
SCORE_DIFF_GAP = 80
# How far right of a LONE team code the two scores may sit, and how far apart
# they must be. Tight enough that the clock block (x ~1300+) can never qualify.
SCORE_ANCHOR_SPAN = 450
SCORE_ONE_CODE_GAP = 60

# Uppercase tokens that LOOK like a team code but are not one.
NOT_A_CODE = {
    "ST", "ND", "RD", "TH", "AND", "GOAL", "VS", "SNP", "EA", "NFL", "OT",
    "ARCADE", "COMP", "PREPLAY", "SUBS", "MADDEN", "SPORTS", "TOTAL", "QTR",
    "FLAG", "TD", "FG", "PAT", "AM", "PM", "SEC", "MIN", "KICK", "PUNT",
    "ILY", "ALU", "TLU", "ILU", "TU", "NU",     # Vision's readings of the NY logo
}

# ⭐⭐ THE 32 TEAM CODES, AS A WHITELIST.
# NOT_A_CODE above is a BLOCKLIST, and a blocklist of OCR garbage can never be
# complete: "TST" and "75T" (both manglings of "1ST") sailed through it and were
# taken for team codes. That widened the score span to the right and swallowed
# the PLAY CLOCK, the down and the distance - which is how a first-quarter game
# reported ours=39 with play=39, and ours=19 from "1st & 10".
#
# There are only 32 teams. Anything else on that bar is not a team, whatever it
# looks like. An unrecognised code now yields NO score - an honest "look again"
# rather than a plausible wrong number that the validators will happily accept.
TEAM_CODES = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LV", "LAC", "LAR", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
    # Madden/Vision variants seen or plausible
    "JAC", "WSH", "GNB", "KAN", "LVR", "NWE", "NOR", "SFO", "TAM",
}

# ⛔⛔ OUR SIDE IS LABELLED WITH THE GAMERTAG, NOT A TEAM CODE.
# The bar reads "DET 0 x 6 JOH" - "JOH" is JohnWick_OGX, not a team.
# With only ONE recognised code on the row the parser cannot pair the scores, so
# it returned nothing, the tally sat at 0-0, and the Q4 bail-out PAUSED A GAME
# WE WERE WINNING because 0-0 in the fourth looks like a tie.
# Set MUT_EVENT_OUR_CODE if the gamertag prefix ever changes.
# Our side shows EITHER the gamertag ("JOH") or the team code - Raiders now, so
# OAK/LV. All three are ours; accept any of them.
for _c in os.environ.get("MUT_EVENT_OUR_CODES", "JOH OAK LV LVR").split():
    TEAM_CODES.add(_c.upper())

_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
_PLAYCLOCK = re.compile(r"^:?(\d{1,2})$")
_INT = re.compile(r"^(\d{1,3})$")
_CODE = re.compile(r"^[A-Z]{2,4}$")
# ⛔ Vision renders "1ST" as "IST" or "LST" - a capital i or l, not a one. This
# exact substitution cost the old build a working down-reader.
_ORDINAL = re.compile(r"^([1-4ILil])(?:ST|ND|RD|TH)$", re.I)
_AMP = ("&", "8", "GOAL", "6")     # Vision renders "&" as "8" much of the time


def _clean(tok):
    t = tok
    for junk in ("•", "×", "▴", "▾", "▸", "◂", "|", ",", "_", "·", "'", "+"):
        t = t.replace(junk, "")
    return t.strip()


def _clean_code(tok):
    """Cleaner for TEAM CODES and SCORES only.

    ⛔ Separate from _clean on purpose: this also strips ":" and ".", which the
    score bar draws as separators ("0 | ABC : [logo]"), and which left a code
    like "ABC:" failing the team-code pattern so no score could be paired with it.
    _clean must NOT strip ":" - the clock tokens "1:17" and ":34" need it.
    """
    return _clean(tok).strip(":.").strip()


def _ordinal_value(tok):
    m = _ORDINAL.match(_clean(tok))
    if not m:
        return None
    c = m.group(1).upper()
    return 1 if c in ("I", "L") else int(c)


def rows_of(boxes):
    """Group boxes into visual rows, each ordered left to right.

    Returns [[(text, cx), ...], ...] top row first.
    """
    items = []
    for b in boxes:
        text = b["text"]
        if not text.strip():
            continue
        width = max(1.0, b["x1"] - b["x0"])
        n = max(1, len(text))
        pos = 0
        for tok in text.split():
            idx = text.find(tok, pos)
            if idx < 0:
                idx = pos
            pos = idx + len(tok)
            cx = b["x0"] + ((idx + len(tok) / 2.0) / n) * width
            items.append((tok, cx, b["cy"]))
    # ⛔⛔ DEDUPE BEFORE GROUPING. Merging several passes means the SAME token
    # arrives several times, and every rule here is positional - "the play clock
    # is the token after the game clock", "the down is the ordinal before the
    # &". Duplicates insert themselves between a token and its neighbour and
    # silently break both. (Measured: merging passes fixed the scores and
    # simultaneously lost the play clock and the down.)
    #
    # Two readings that DISAGREE ("2ND" vs "200") are not duplicates and both
    # survive on purpose - the shape regexes pick the valid one, which is the
    # whole benefit of reading several times.
    deduped = []
    for tok, cx, cy in items:
        key = _clean(tok).upper()
        if any(_clean(t).upper() == key and abs(x - cx) < 14 and abs(y - cy) < 10
               for t, x, y in deduped):
            continue
        deduped.append((tok, cx, cy))
    items = deduped

    items.sort(key=lambda t: t[2])
    rows = []
    for tok, cx, cy in items:
        if rows and abs(rows[-1][0] - cy) <= ROW_TOL:
            rows[-1][1].append((tok, cx))
        else:
            rows.append((cy, [(tok, cx)]))
    return [sorted(r[1], key=lambda t: t[1]) for r in rows]


_TICKER_WORDS = ("RUSH", "RUSHES", "YDS", "YARDS", "REC", "RECS", "PASS",
                 "COMP", "ATT", "CAR", "TKL", "TKLS", "SACK", "SACKS", "INT",
                 "INTS", "CATCH", "CATCHES", "TD", "TDS")


def parse_hud(boxes):
    """-> dict(quarter, game, play, down, theirs, ours). Any field may be None.

    None means "not readable right now", which is a NORMAL answer - cutscenes,
    replays and the two-minute-warning overlay hide the HUD entirely.
    """
    res = {"quarter": None, "game": None, "play": None, "down": None,
           "goal": False, "yardline": None, "yard_arrow": None,
           "yard_raw": None,
           "theirs": None, "ours": None, "clock_x": None, "overtime": False}
    rows = rows_of(boxes)

    # Game clock first - it anchors which row the quarter is on.
    clock_row = None
    for row in rows:
        for i, (tok, _x) in enumerate(row):
            m = _CLOCK.match(_clean(tok))
            if not m or res["game"] is not None:
                continue
            res["game"] = int(m.group(1)) * 60 + int(m.group(2))
            res["clock_x"] = round(_x)
            clock_row = row
            # The play clock is the next clock-shaped token on the same row.
            for tok2, _x2 in row[i + 1:i + 4]:
                m2 = _PLAYCLOCK.match(_clean(tok2))
                if m2 and 0 <= int(m2.group(1)) <= 40:
                    res["play"] = int(m2.group(1))
                    break
            break

    # ⭐⭐ OVERTIME IS A QUARTER-SLOT READING, NOT A TOKEN ANYWHERE ON SCREEN.
    # The HUD puts "OT" where the ordinal would be, on the CLOCK ROW.
    #
    # ⛔ Scanning all the merged text for "OT" false-positived while we led 34-5
    # - some fragment on the special-teams screen matched. That is
    # expensive: `overtime` is sticky for the game and it switches OFF both the
    # clock chewing and the quarter-end skip, so a false positive quietly costs
    # minutes on a game we are already winning.
    if clock_row is not None:
        for tok, _x in clock_row:
            if _clean_code(tok).upper() == "OT":
                res["overtime"] = True
                break

    # ⭐ THE DOWN IS THE ORDINAL FOLLOWED BY "&" OR "GOAL" ON THE SAME ROW; the
    # quarter never is. Both render as "<digit><suffix>", which is why anything
    # simpler confuses them.
    #
    # ⭐ AND THE QUARTER SITS ON THE CLOCK'S ROW in every layout seen. Preferring
    # that row matters once passes are merged: the centred layout puts
    # "1ST & GOAL" on its own row, and a pass that read "IST" but missed the "&"
    # beside it would otherwise be taken for a first quarter. (Measured: it was.)
    for row in rows:
        for i, (tok, _x) in enumerate(row):
            v = _ordinal_value(tok)
            if v is None:
                continue
            # ⛔ LOOK AT THE NEXT TWO TOKENS, not just one. Merging passes can
            # wedge a garbage reading between a token and its neighbour -
            # measured: "2ND | 2NG970 | 8 | 10", where the junk hid the "&" and
            # the down went unread.
            nxts = [_clean(t).upper() for t, _ in row[i + 1:i + 3]]
            if any(n.startswith(a) for n in nxts for a in _AMP):
                if res["down"] is None:
                    res["down"] = v
                    # ⭐ GOAL TO GO. "1ST & GOAL" instead of "1ST & 10" is the
                    # cheapest possible read of field position: it is already on
                    # this row, already parsed, and needs no new geometry and no
                    # arrow-direction logic.
                    #
                    # ⛔ Vision renders "GOAL" as "60AL"/"GDAL" and "&" as "8",
                    # so match on a LOOSE prefix over the next few tokens rather
                    # than on equality - the same discipline _AMP exists for.
                    tail = " ".join(nxts + [_clean(t).upper()
                                            for t, _ in row[i + 3:i + 4]])
                    res["goal"] = any(g in tail for g in
                                      ("GOAL", "GOA", "60AL", "GDAL", "G0AL"))

                    # ⭐ FIELD POSITION (recorded, not acted on). It renders after the play clock as an arrow plus
                    # a yard line - "▼ 32", "▲ 26" - and Vision mangles the
                    # arrow into ^, ~, v, or drops it entirely.
                    # ⛔ DIRECTION IS THE WHOLE RULE: down-arrow is OUR half,
                    # up-arrow is theirs. The raw arrow token is returned;
                    # never infer a direction that was not actually read.
                    after = [(_clean(t), x) for t, x in row[i + 3:]]
                    for tok, _x in after:
                        m_fp = re.match(r"^([\^~v\u25b2\u25bc]?)\s*(\d{1,2})$",
                                        tok.strip())
                        if m_fp and res.get("yardline") is None:
                            res["yardline"] = int(m_fp.group(2))
                            res["yard_arrow"] = m_fp.group(1) or "?"
                            res["yard_raw"] = tok.strip()
            elif clock_row is not None and row is clock_row:
                if res["quarter"] is None:
                    res["quarter"] = v
    if res["quarter"] is None:
        # No ordinal on the clock row (or no clock at all) - fall back to any
        # ordinal that is not a down.
        for row in rows:
            for i, (tok, _x) in enumerate(row):
                v = _ordinal_value(tok)
                nxts = [_clean(t).upper() for t, _ in row[i + 1:i + 3]]
                if v is not None and not any(n.startswith(a)
                                             for n in nxts for a in _AMP):
                    res["quarter"] = v
                    break
            if res["quarter"] is not None:
                break

    # ⛔ STAT TICKER (Oct 16): after some plays the scorebar is replaced by a
    # player line ("LADAINIAN TOMLINSON | 8 RUSH | 32 YDS"); while it animates,
    # its numbers were paired with a team code and read as OUR score (0 -> 6/7,
    # corrected a play later - meanwhile the wrong offence ran). Any ticker word
    # in the band = no score this read; the next read gets it.
    _all = " ".join(_clean(t).upper() for row in rows for t, _ in row)
    if any(re.search(rf"\b{w}\b", _all) for w in _TICKER_WORDS):
        return res

    # ⭐ SCORES ARE PAIRED WITH THEIR TEAM CODE ON THE SAME ROW, never with a
    # fixed x. The LEFT code is the opponent and the RIGHT code is us in every
    # layout seen. The clock is deliberately NOT used as the divider - which
    # side of it the scores sit on is exactly what changes between layouts.
    for row in rows:
        # ⛔ WHITELIST, not blocklist - see TEAM_CODES. A token that merely
        # LOOKS like a code (TST, 75T, ILU) is not one.
        codes = [(t, x) for t, x in row
                 if _clean_code(t).upper() in TEAM_CODES]
        # ⛔ ONE CODE IS ENOUGH TO ANCHOR ON. Some presentations show
        # the opponent's abbreviation but render OUR side as a logo with no
        # text, so requiring two codes threw the whole row away - the tally sat
        # at 0-0 and the Q4 check paused a game we were winning 7-3.
        if not codes:
            continue
        nums = []
        for i, (tok, x) in enumerate(row):
            c = _clean_code(tok)
            prev = _clean(row[i - 1][0]).upper() if i else ""
            if any(prev.startswith(a) for a in _AMP):
                continue                      # "& 10" belongs to the down
            if _INT.match(c):
                nums.append((int(c), x))
            elif c.upper() in ("O", "U"):
                # A lone zero renders as the letter O - measured repeatedly, it
                # is the ONE value Vision drops rather than misreads.
                # ⛔ And as "U" on the boxed-score presentation ("MIA 3
                # | U JOH" for 3-0, every read of a whole losing Q4 - no score
                # was ever recorded, and the Q4 alarm never fired).
                nums.append((0, x))
        if len(nums) < 2:
            continue
        left, right = codes[0], codes[-1]

        # ⛔⛔ THE SCORES LIE *BETWEEN* THE TWO TEAM CODES. NOTHING ELSE COUNTS.
        # Pairing each
        # code with its NEAREST number puts our score on whatever token happens
        # to sit closest to the right-hand code - and on the wide presentation
        # that is the play clock, just outside the bar:
        #     [BUF] 3 x 0 [NYG] | 3rd 0:17 | :05 | 2ND & 7 | ^44
        # Measured live, first quarter: ours=39 with play=39, and ours=20 with
        # play=20. A play clock makes a PLAUSIBLE score, so the validators let
        # it through, the never-decrease guard then locked it in, and the Q4
        # bail-out spent the rest of the game believing we were comfortably
        # ahead. Most of today's phantom scores - 34, 38, 30, 20, 39 - were
        # play clocks.
        #
        # Position is structural, not a guess: the clocks and the down are
        # OUTSIDE the two codes, the scores are INSIDE. Filter on that first.
        lo, hi = min(left[1], right[1]), max(left[1], right[1])
        inner = [n for n in nums if lo < n[1] < hi]

        # ⛔⛔ SOME PRESENTATIONS SHOW ONLY ONE TEAM CODE.
        # Measured live: "TEN@560 | 3@695 | 7@799 | ... | 4th@1318 | 2:56@1391".
        # The opponent had a text code; OUR side was a logo with no text. With
        # one code there is no span to bracket, so the parser returned nothing,
        # the tally sat at 0-0, and the Q4 check PAUSED A GAME WE WERE WINNING
        # 7-3. That is the false-alarm failure mode, not a safe one.
        #
        # The scores sit immediately right of the code we DO have, well clear of
        # the clock block further right. So anchor on the single code and take
        # the first two numbers beside it - bounded by distance so the game
        # clock, play clock and distance-to-go can never qualify.
        one_code = False
        if len(inner) < 2 and len(codes) == 1:
            near = sorted((n for n in nums
                           if 0 < n[1] - left[1] <= SCORE_ANCHOR_SPAN),
                          key=lambda n: n[1])
            if len(near) >= 2 and (near[1][1] - near[0][1]) >= SCORE_ONE_CODE_GAP:
                inner = near[:2]
                one_code = True

        if len(inner) < 2:
            continue                 # honest "look again", never a guess
        theirs = min(inner, key=lambda n: n[1])
        ours = max(inner, key=lambda n: n[1])
        # ⛔ THE OPPONENT'S SCORE IS ALWAYS LEFT OF OURS. Refuse anything else.
        # (Measured live, with the opponent's "0" unread, the left code
        # latched onto OUR score and reported theirs=14 while we led 14-0. The
        # score validator rejected it downstream, but a mis-pairing should never
        # get that far - it only has to slip through once, at a moment the
        # validator happens to accept, to flip the Q4 bail-out or the recorded
        # result.)
        #
        # Requiring strict left-of-right ordering costs nothing when both scores
        # are legible and correctly returns None when only one is - which is an
        # honest "look again", not a wrong answer.
        # ⛔⛔ THE TWO SCORES MUST BE GENUINELY FAR APART ON SCREEN.
        # (reported a 20-20 TIE during a 20-0 win, which paused the
        # game and made the player take over for nothing.)
        #
        # A real ZERO is the one value OCR loses rather than misreads - it
        # vanishes. With the opponent's 0 unread, the merged passes can yield
        # OUR score twice at slightly different x, and the left code then pairs
        # to one copy while the right code pairs to the other. The result looks
        # like a perfectly plausible tie.
        #
        # Two genuine scores sit on opposite sides of the bar. Two copies of the
        # same numeral sit almost on top of each other. So require real
        # separation, and return None rather than invent a tie.
        # ⛔ SCORE_MIN_GAP guards against the SAME numeral being read twice at
        # slightly different x and pairing into a phantom tie. On the one-code
        # path the two scores sit closer together (measured: 104px) because we
        # are anchored beside a single code rather than spanning the whole bar,
        # so that threshold would throw away a perfectly good reading.
        _gap_needed = SCORE_ONE_CODE_GAP if one_code else (
            SCORE_MIN_GAP if theirs[0] == ours[0] else SCORE_DIFF_GAP)
        if theirs[1] < ours[1] and (ours[1] - theirs[1]) >= _gap_needed:
            res["theirs"], res["ours"] = theirs[0], ours[0]
            break
    return res


def _sub_image(img, rect):
    return Quartz.CGImageCreateWithImageInRect(
        img, Quartz.CGRectMake(rect[0], rect[1], rect[2], rect[3]))


def _load_image(path):
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    return Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)


def merged_boxes(path, scales=HUD_SCALES, subcrops=HUD_SUBCROPS):
    """Every (sub-crop x scale) pass, merged into BAND coordinates."""
    base = _load_image(path)
    if base is None:
        return []
    out = []
    for (ox, oy, w, h) in subcrops:
        img = base if (ox, oy) == (0, 0) and w >= Quartz.CGImageGetWidth(base) \
            else _sub_image(base, (ox, oy, w, h))
        if img is None:
            continue
        for sc in scales:
            for b in screen.ocr_boxes_image(img, scale=sc):
                b = dict(b)
                b["x0"] += ox
                b["x1"] += ox
                b["cx"] += ox
                b["cy"] += oy
                out.append(b)
    return out


def read_hud_file(path, scales=HUD_SCALES):
    """⭐ UNION for recall, then the regexes for precision.

    Merging every pass maximises the chance each value is seen at all, and the
    shape rules (an ordinal followed by "&" is a down; m:ss is a game clock; a
    score sits beside its team code) throw out the garbage that comes with it.
    Scores additionally go through grind.py's validator - monotonic, bounded
    jump, confirm-twice - which is the layer that has actually caught bad reads.
    """
    return parse_hud(merged_boxes(path, scales=scales))


def read_hud(path=None, scales=HUD_SCALES):
    """ONE capture -> the whole HUD.

    ⭐ One capture, several recognitions: the clock and the score can never
    disagree about which moment they describe, because the screen cannot move
    between them.
    """
    path = path or f"{C.SHOT_DIR}/hud.png"
    pad.shot(HUD_BAND, path)
    return read_hud_file(path, scales=scales)


# ---------------------------------------------------------------------------
# WHICH PRESENTATION IS THIS?
# ---------------------------------------------------------------------------
# Madden has several broadcast presentations (the default wide bar, a centred
# primetime-style block, ...), and a game can use any of them. The reader is
# content-based so it does not care; the layout is only RECORDED in the history.
#
# The clock's x-centre is the cheapest stable fingerprint we already compute.
FAST_SUBCROPS = ((0, 0, 1920, 130), (480, 10, 960, 120))
FAST_SCALES = (2,)


def layout_name(clock_x):
    """A coarse label for the presentation, or None.

    Deliberately buckets rather than guesses a broadcast name - we have seen
    two layouts and should not invent labels for packages we have not observed.
    """
    if clock_x is None:
        return None
    if clock_x >= 1200:
        return "wide"        # the default bar
    if clock_x >= 700:
        return "centred"     # primetime-style compact block
    return f"other@{clock_x}"


def read_clocks_fast(path=None):
    """Clock only, cheap. ~0.4-0.6s vs ~1.3s for the full read.

    The chew loop polls this several times per snap, so the full six-pass read
    would cost more real time than chewing saves.
    """
    path = path or f"{C.SHOT_DIR}/hud.png"
    pad.shot(HUD_BAND, path)
    return parse_hud(merged_boxes(path, scales=FAST_SCALES,
                                  subcrops=FAST_SUBCROPS))


# ---------------------------------------------------------------------------
# THE FINAL SCORE, FROM THE SCREEN THAT SAYS THE GAME IS OVER
# ---------------------------------------------------------------------------
# ⛔⛔ WHY THIS EXISTS. The result
# used to come from the last in-play score the loop happened to accept before it
# decided the game had ended. Two things then went wrong at once:
#
#   1. The game-end was inferred from "the menu walk took >25s", which fired on a
#      cutscene mid-game and SPLIT one game into two records.
#   2. The in-play score read at that moment was wrong - it recorded 12-2 as a
#      LOSS when the scoreboard said GB 0 - 12 NY, a twelve-point WIN.
#
# The end-of-game banner shows the final score in huge unambiguous numerals.
# Read THAT. It is the one moment the score is not competing with a play clock,
# a down marker, a possession arrow or a cycling stat pop-up.
# ⛔ WIDE ON PURPOSE. The first version of this crop was (850, 80, 560, 150),
# measured against ONE frame - the same fixed-coordinate mistake that cost the
# in-play HUD half a morning. It returned "banner score unreadable" on the very
# next game, because the presentation had changed. Cover the whole banner and
# find the numbers by what they LOOK like.
FINAL_BANNER = (380, 30, 1300, 240)

# The final score is drawn MUCH larger than anything else on that banner. Height
# is the discriminator: the scores tower over "METLIFE STADIUM", the quarter, the
# down and the key-player stats.
FINAL_MIN_H = 45


def read_final_score(path=None, save_debug=False):
    """(theirs, ours) off the end-of-game banner, or None.

    ⭐ THE TWO BIGGEST NUMBERS ON THE BANNER, left and right. Left is the
    away/opponent side, right is ours - the same ordering the in-play bar uses.

    ⛔ Requires exactly two, so a half-drawn banner returns None rather than half
    an answer. None is safe: the caller falls back to the in-play score and the
    game is still recorded.
    """
    path = path or f"{C.SHOT_DIR}/final.png"
    pad.shot(FINAL_BANNER, path)
    return parse_final_score(path)


def parse_final_score(path):
    cands = []
    for sc in (2, 3):
        for b in screen.ocr_boxes_file(path, scale=sc):
            h = b["y1"] - b["y0"]
            if h < FINAL_MIN_H:
                continue                      # ordinary text, not a score
            for tok in b["text"].split():
                c = _clean_code(tok)
                if _INT.match(c):
                    cands.append((int(c), b["x0"], h))
                elif c.upper() == "O":
                    # A lone zero renders as the letter O - the one value Vision
                    # drops rather than misreads.
                    cands.append((0, b["x0"], h))
    if not cands:
        return None
    # ⛔ CLUSTER, don't dedupe pairwise. The same numeral read at two scales
    # lands up to ~80px apart (the box origin shifts), while the two scores sit
    # ~160px apart - so a simple "skip if within 80px" splits one score into two
    # entries and the count check then fails. Measured on a real frame: 0@559,
    # 12@720, 12@801 was read as THREE scores.
    cands.sort(key=lambda n: n[1])
    clusters = []
    for v, x, h in cands:
        if clusters and (x - clusters[-1][-1][1]) <= 120:
            clusters[-1].append((v, x, h))
        else:
            clusters.append([(v, x, h)])
    if len(clusters) != 2:
        return None
    out = []
    for cl in clusters:
        vals = Counter(v for v, _x, _h in cl)
        out.append(vals.most_common(1)[0][0])
    return out[0], out[1]
