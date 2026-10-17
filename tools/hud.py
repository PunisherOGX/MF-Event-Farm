#!/usr/bin/env python3
"""Read the live HUD by CONTENT, not by fixed coordinates.

Madden uses more than one scoreboard layout (a wide bar across the bottom; a
compact centred block with the down on a second row), and they put the same
values in different places. A crop set per layout cannot keep up, so values
are found by WHAT THEY LOOK LIKE and where they sit RELATIVE TO EACH OTHER:

  * ROWS: tokens are grouped by y first, then ordered by x, so a down on the
    row above the clock never interleaves with the quarter.
  * MULTI-PASS VOTING: several sub-crops x upscales are OCR'd and merged;
    Vision's failures are not correlated across them, and the shape regexes
    throw out the garbage that comes with the extra recall.
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

# Sub-crops (band-relative; offsets are added back so every pass lands in one
# coordinate space). No single crop reliably carries all four score tokens.
HUD_SUBCROPS = ((0, 0, 1920, 130), (380, 0, 1250, 130),
                (480, 10, 960, 120))
HUD_SCALES = (2, 3)

ROW_TOL = 14          # px: how close two boxes must be in y to be one row
# Minimum x separation between the two scores. Copies of the SAME numeral land
# within a few px of each other; the real pair straddles the bar (~115px+).
# 120 only where the values are EQUAL (guards a phantom tie); 80 otherwise.
SCORE_MIN_GAP = 120
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

# THE 32 TEAM CODES, AS A WHITELIST. A blocklist of OCR garbage is never
# complete ("TST"/"75T" from "1ST" were taken for codes and swallowed the play
# clock as a score). An unrecognised code yields NO score: "look again".
TEAM_CODES = {
    "ARI", "ATL", "BAL", "BUF", "CAR", "CHI", "CIN", "CLE", "DAL", "DEN",
    "DET", "GB", "HOU", "IND", "JAX", "KC", "LV", "LAC", "LAR", "MIA",
    "MIN", "NE", "NO", "NYG", "NYJ", "PHI", "PIT", "SEA", "SF", "TB",
    "TEN", "WAS",
    # Madden/Vision variants seen or plausible
    "JAC", "WSH", "GNB", "KAN", "LVR", "NWE", "NOR", "SFO", "TAM",
}

# ⛔ OUR SIDE MAY BE LABELLED WITH THE GAMERTAG, NOT A TEAM CODE: the bar can
# read "DET 0 x 6 ABC" where ABC is the first three letters of your gamertag.
# Put every label your side shows (gamertag prefix, team code) in
# MUT_EVENT_OUR_CODES, space-separated, e.g. MUT_EVENT_OUR_CODES="ABC LV".
for _c in os.environ.get("MUT_EVENT_OUR_CODES", "").split():
    TEAM_CODES.add(_c.upper())

_CLOCK = re.compile(r"^(\d{1,2}):(\d{2})$")
_PLAYCLOCK = re.compile(r"^:?(\d{1,2})$")
_INT = re.compile(r"^(\d{1,3})$")
_CODE = re.compile(r"^[A-Z]{2,4}$")
# Vision renders "1ST" as "IST" or "LST" - a capital i or l, not a one.
_ORDINAL = re.compile(r"^([1-4ILil])(?:ST|ND|RD|TH)$", re.I)
_AMP = ("&", "8", "GOAL", "6")     # Vision renders "&" as "8" much of the time


def _clean(tok):
    t = tok
    for junk in ("•", "×", "▴", "▾", "▸", "◂", "|", ",", "_", "·", "'", "+"):
        t = t.replace(junk, "")
    return t.strip()


def _clean_code(tok):
    """Cleaner for TEAM CODES and SCORES only.

    Also strips ":" and ".", which the score bar draws as separators ("ABC:").
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
    # DEDUPE BEFORE GROUPING: merged passes repeat tokens, and every rule here
    # is positional ("the play clock follows the game clock"). Readings that
    # DISAGREE ("2ND" vs "200") both survive; the shape regexes pick one.
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


def parse_hud(boxes):
    """-> dict(quarter, game, play, down, theirs, ours, clock_x, overtime).

    None means "not readable right now", which is a NORMAL answer - cutscenes,
    replays and overlays hide the HUD entirely.
    """
    res = {"quarter": None, "game": None, "play": None, "down": None,
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

    # OVERTIME is a quarter-slot reading on the CLOCK ROW, never a token
    # anywhere on screen (a loose scan false-positived; `overtime` is sticky
    # and switches off chewing and the quarter-end skip).
    if clock_row is not None:
        for tok, _x in clock_row:
            if _clean_code(tok).upper() == "OT":
                res["overtime"] = True
                break

    # THE DOWN is the ordinal followed by "&" or "GOAL" on the same row; the
    # quarter never is. THE QUARTER sits on the clock's row in every layout.
    for row in rows:
        for i, (tok, _x) in enumerate(row):
            v = _ordinal_value(tok)
            if v is None:
                continue
            # The next TWO tokens: merged passes can wedge junk before the "&".
            nxts = [_clean(t).upper() for t, _ in row[i + 1:i + 3]]
            if any(n.startswith(a) for n in nxts for a in _AMP):
                if res["down"] is None:
                    res["down"] = v
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

    # SCORES are paired with their team code on the same row, never with a
    # fixed x. The LEFT code is the opponent and the RIGHT code is us. One code
    # is enough to anchor on (some presentations show our side as a logo).
    for row in rows:
        codes = [(t, x) for t, x in row
                 if _clean_code(t).upper() in TEAM_CODES]
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
                # A lone zero renders as the letter O (or U on the boxed-score
                # presentation).
                nums.append((0, x))
        if len(nums) < 2:
            continue
        left, right = codes[0], codes[-1]

        # The scores lie BETWEEN the two team codes; the clocks and the down
        # are outside them (nearest-number pairing took the play clock).
        lo, hi = min(left[1], right[1]), max(left[1], right[1])
        inner = [n for n in nums if lo < n[1] < hi]

        # ONE code only: take the first two numbers right of it, bounded by
        # distance so the clock block can never qualify.
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
        # The opponent's score is always LEFT of ours, and the two must be
        # genuinely apart on screen: an unread 0 can leave OUR score read twice
        # at nearby x, which would pair into a phantom tie. The one-code path
        # sits closer together (~104px), so it has its own gap.
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
    """UNION for recall, then the shape regexes for precision. Scores also go
    through grind.py's validator (bounded jumps, confirm-to-lower)."""
    return parse_hud(merged_boxes(path, scales=scales))


def read_hud(path=None, scales=HUD_SCALES):
    """ONE capture -> the whole HUD, so the clock and the score always
    describe the same moment."""
    path = path or f"{C.SHOT_DIR}/hud.png"
    pad.shot(HUD_BAND, path)
    return read_hud_file(path, scales=scales)


# ---------------------------------------------------------------------------
# WHICH PRESENTATION IS THIS?
# ---------------------------------------------------------------------------
# The reader is content-based, so the layout is only RECORDED in the history,
# fingerprinted by the clock's x-centre.
FAST_SUBCROPS = ((0, 0, 1920, 130), (480, 10, 960, 120))
FAST_SCALES = (2,)


def layout_name(clock_x):
    """A coarse label for the presentation, or None."""
    if clock_x is None:
        return None
    if clock_x >= 1200:
        return "wide"        # the default bar
    if clock_x >= 700:
        return "centred"     # primetime-style compact block
    return f"other@{clock_x}"


def read_clocks_fast(path=None):
    """Clock only, cheap (~0.5s vs ~1.3s): the chew loop polls this."""
    path = path or f"{C.SHOT_DIR}/hud.png"
    pad.shot(HUD_BAND, path)
    return parse_hud(merged_boxes(path, scales=FAST_SCALES,
                                  subcrops=FAST_SUBCROPS))


# ---------------------------------------------------------------------------
# THE FINAL SCORE, FROM THE SCREEN THAT SAYS THE GAME IS OVER
# ---------------------------------------------------------------------------
# The end-of-game banner shows the final score in huge unambiguous numerals;
# the in-play score misreads. The crop is WIDE on purpose (the banner moves
# between presentations): numbers are found by what they look like.
FINAL_BANNER = (380, 30, 1300, 240)

# The final score is drawn MUCH larger than anything else on that banner.
FINAL_MIN_H = 45


def read_final_score(path=None, save_debug=False):
    """(theirs, ours) off the end-of-game banner, or None.

    The two biggest numbers on the banner: left is the opponent, right is ours.
    Requires exactly two; None is safe (the caller keeps the in-play score).
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
    # CLUSTER, don't dedupe pairwise: the same numeral read at two scales lands
    # up to ~80px apart, while the two scores sit ~160px apart.
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
