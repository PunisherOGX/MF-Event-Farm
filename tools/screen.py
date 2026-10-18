#!/usr/bin/env python3
"""Everything that READS the screen. No button ever gets pressed from here.

⭐ THE HABIT THIS FILE SERVES: look at the screen, do not reason about what the
code probably does. Every long debugging session on this project came from
describing behaviour instead of capturing a frame - and each was then solved in
seconds by looking. Reading is cheap; keep it that way and use it constantly.

⛔ NEVER shell out to `screencapture`. It costs ~1.9s per call - fixed overhead,
not pixel work - and it returns frames that are SECONDS OUT OF DATE. The pad
daemon captures in-process via CoreGraphics in ~0.03s. That one change took
games from 32 minutes to 17.
"""
import hashlib
import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import CoreFoundation  # noqa: E402
import Quartz  # noqa: E402
import Vision  # noqa: E402
from Foundation import NSURL  # noqa: E402

import config as C  # noqa: E402
import pad  # noqa: E402
import ocr  # noqa: E402

os.makedirs(C.SHOT_DIR, exist_ok=True)
os.makedirs(C.STUCK_DIR, exist_ok=True)

# Consecutive byte-identical tab-strip captures. See config.FROZEN_RESTART.
_last_frame_hash = None
FROZEN_STREAK = 0


# ---------------------------------------------------------------------------
# Pixel helpers - CoreGraphics, no PIL
# ---------------------------------------------------------------------------

def load(path):
    """Return (bytes, width, height, bytes_per_row, bytes_per_pixel)."""
    raw = path.encode()
    url = CoreFoundation.CFURLCreateFromFileSystemRepresentation(
        None, raw, len(raw), False)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    provider = Quartz.CGImageGetDataProvider(img)
    data = bytes(Quartz.CGDataProviderCopyData(provider))
    return (data,
            Quartz.CGImageGetWidth(img),
            Quartz.CGImageGetHeight(img),
            Quartz.CGImageGetBytesPerRow(img),
            Quartz.CGImageGetBitsPerPixel(img) // 8)


def band_x(buf, w, h, bpr, bpp, x0, x1):
    """Mean luminance of the vertical band [x0,x1), sampled every 2nd pixel."""
    x0, x1 = max(0, int(x0)), min(w, int(x1))
    total = n = 0
    for y in range(0, h, 2):
        row = y * bpr
        for x in range(x0, x1, 2):
            i = row + x * bpp
            total += buf[i] + buf[i + 1] + buf[i + 2]
            n += 3
    return total / n if n else 0.0


def band_y(buf, w, h, bpr, bpp, y0, y1):
    """Mean luminance of the horizontal band [y0,y1).

    ⭐ The row-wise twin of band_x, and the whole reason the entry-options
    screen can be navigated by verification instead of by assumption: a focused
    menu row is drawn as a light box, exactly like an active tab.
    """
    y0, y1 = max(0, int(y0)), min(h, int(y1))
    total = n = 0
    for y in range(y0, y1, 2):
        row = y * bpr
        for x in range(0, w, 2):
            i = row + x * bpp
            total += buf[i] + buf[i + 1] + buf[i + 2]
            n += 3
    return total / n if n else 0.0


def box_mean(buf, w, h, bpr, bpp, x0, x1, y0, y1):
    """Mean luminance of a RECTANGLE, sampled every 2nd pixel.

    ⛔ THIS EXISTS BECAUSE band_y WAS WRONG FOR MENU ROWS. (Measured live on the
    ENTRY OPTIONS screen.) band_y averages the FULL WIDTH of the screen,
    but a Madden menu button is ~430px of a 1920px-wide display - so ~78% of
    every sample was background, and a genuinely highlighted row read 41.7
    against an unhighlighted 43.4. The highlight was real and plainly visible in
    the screenshot; the measurement was averaging it away.

    Sample the BUTTON, not the row.
    """
    x0, x1 = max(0, int(x0)), min(w, int(x1))
    y0, y1 = max(0, int(y0)), min(h, int(y1))
    total = n = 0
    for y in range(y0, y1, 2):
        row = y * bpr
        for x in range(x0, x1, 2):
            i = row + x * bpp
            total += buf[i] + buf[i + 1] + buf[i + 2]
            n += 3
    return total / n if n else 0.0


def col_profile(buf, w, h, bpr, bpp):
    """Mean luminance per column, as [(x, value)]."""
    out = []
    for x in range(0, w, 2):
        total = n = 0
        for y in range(0, h, 2):
            i = y * bpr + x * bpp
            total += buf[i] + buf[i + 1] + buf[i + 2]
            n += 3
        out.append((x, total / n))
    return out


# ---------------------------------------------------------------------------
# OCR WITH POSITIONS
# ---------------------------------------------------------------------------

def ocr_boxes(region=None, path=None, scale=2.0):
    """OCR a region and return WHERE each string was found.

    Returns [{text, x0, y0, x1, y1, cx, cy, fy}] in PIXELS of the capture,
    plus `fy` as a 0-1 fraction down the image. Top-left origin, like every
    other coordinate in this project.

    ⭐ WHY THIS EXISTS. `step.ocr` returns a pipe-joined string with no
    positions, which forces every menu to be navigated by hardcoded row
    geometry or by assuming what has focus. Assuming what had focus on the
    entry-options screen sent ~11 tier rewards down the wrong path last event.
    With positions we can find an option by its WORDS wherever it sits, then
    measure whether that row is the highlighted one - so the code survives the
    layout moving, which is exactly what the player asked for.

    ⛔ Vision's boundingBox is normalised with a BOTTOM-LEFT origin. The flip is
    done here, once, so no caller has to remember it.
    """
    path = path or f"{C.SHOT_DIR}/_boxes.png"
    pad.shot(region, path)
    return ocr_boxes_file(path, scale=scale)


def ocr_boxes_image(img, scale=2.0):
    """ocr_boxes against an in-memory CGImage.

    ⭐ Lets several sub-crops of ONE capture be recognised without decoding the
    PNG again for each - the HUD reader makes four passes per look.
    """
    if img is None:
        return []
    W = Quartz.CGImageGetWidth(img)
    H = Quartz.CGImageGetHeight(img)
    if scale and scale != 1:
        img = ocr.upscale(img, scale)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        img, None)
    handler.performRequests_error_([req], None)
    out = []
    for obs in (req.results() or []):
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        bb = obs.boundingBox()
        x0 = bb.origin.x * W
        x1 = (bb.origin.x + bb.size.width) * W
        y0 = (1.0 - (bb.origin.y + bb.size.height)) * H
        y1 = (1.0 - bb.origin.y) * H
        out.append({"text": cands[0].string(), "x0": x0, "x1": x1,
                    "y0": y0, "y1": y1, "cx": (x0 + x1) / 2.0,
                    "cy": (y0 + y1) / 2.0,
                    "fy": ((y0 + y1) / 2.0) / H if H else 0.0,
                    "conf": cands[0].confidence()})
    out.sort(key=lambda b: b["cy"])
    return out


def ocr_boxes_file(path, scale=2.0):
    """ocr_boxes against an ALREADY-CAPTURED file.

    ⭐ Separated on purpose: a decision that compares TEXT with BRIGHTNESS must
    read both from the SAME frame. Capturing twice lets the screen move between
    them, and a menu that moved between the read and the press is precisely the
    failure this is guarding against.
    """
    url = NSURL.fileURLWithPath_(path)
    src = Quartz.CGImageSourceCreateWithURL(url, None)
    img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
    if img is None:
        return []
    W = Quartz.CGImageGetWidth(img)
    H = Quartz.CGImageGetHeight(img)
    if scale and scale != 1:
        img = ocr.upscale(img, scale)
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
        img, None)
    handler.performRequests_error_([req], None)
    out = []
    for obs in (req.results() or []):
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        bb = obs.boundingBox()
        x0 = bb.origin.x * W
        x1 = (bb.origin.x + bb.size.width) * W
        # Bottom-left origin -> top-left.
        y0 = (1.0 - (bb.origin.y + bb.size.height)) * H
        y1 = (1.0 - bb.origin.y) * H
        out.append({
            "text": cands[0].string(),
            "x0": x0, "x1": x1, "y0": y0, "y1": y1,
            "cx": (x0 + x1) / 2.0, "cy": (y0 + y1) / 2.0,
            "fy": ((y0 + y1) / 2.0) / H if H else 0.0,
            "conf": cands[0].confidence(),
        })
    out.sort(key=lambda b: b["cy"])
    return out


def screen_text():
    """Full-screen OCR as one uppercased string.

    Between-game screens have no clock pressure, so they can afford a full
    Vision pass and be identified by their TEXT rather than by pixel geometry.
    That is far more robust and far easier to extend when Madden adds another
    interstitial - and Madden keeps adding them.
    """
    try:
        txt = str(ocr.ocr(None)).upper()
    except Exception:
        return ""
    # A stuck macOS banner ("App Background Activity ... Extensions.") sits
    # over the stream's top-right corner. Its words made loading screens long
    # enough to read as "unknown" -> circle pressed (Oct 15). Drop them.
    txt = _MAC_BANNER.sub("", txt)
    # ⛔ Vision sometimes splits a two-word title into two boxes ("YOUR |
    # REWARDS"); the L3 key pack screen then went unrecognised and was advanced
    # past unopened (Oct 17). Re-join the titles every handler keys on.
    return _SPLIT_TITLE.sub(lambda m: f"{m.group(1)} {m.group(2)}", txt)


_SPLIT_TITLE = re.compile(r"\b(YOUR)\s*\|\s*(REWARDS|PROGRESS)\b")
_MAC_BANNER = re.compile(
    r"\|?\s*APP BACKGROUND ACTIVIT\w*.*?EXTENSIONS\.?\s*", re.S)


# ---------------------------------------------------------------------------
# THE PLAYCALL TAB STRIP
# ---------------------------------------------------------------------------

def read_both(save=None):
    """ONE capture -> (side, index, name, ratio).

    Side is decided by HOW FAR RIGHT the strip's text extends, never by
    brightness alone:
        offense      8 tabs, text out to ~1100
        defense      6 tabs, text out to ~800
        specialteams 3 tabs, text out to ~380
    """
    global _last_frame_hash, FROZEN_STREAK
    out = save or f"{C.SHOT_DIR}/both.png"
    pad.shot(C.TABSTRIP, out)

    try:
        with open(out, "rb") as fh:
            hh = hashlib.md5(fh.read()).hexdigest()
        FROZEN_STREAK = FROZEN_STREAK + 1 if hh == _last_frame_hash else 0
        _last_frame_hash = hh
    except OSError:
        pass

    buf, w, h, bpr, bpp = load(out)
    scale = w / C.TABSTRIP[2]
    cols = col_profile(buf, w, h, bpr, bpp)
    lo = min(c[1] for c in cols)
    hi = max(c[1] for c in cols)
    if hi - lo < 25:
        # A flat band is no tab strip at all - i.e. a LIVE PLAY. This is the
        # cheap, common answer and the caller should treat it as "keep polling",
        # not as a problem.
        return None, -1, "NO-TABSTRIP", 0.0
    thresh = lo + 0.45 * (hi - lo)
    bright = [x for x, v in cols if v >= thresh]
    if not bright:
        return None, -1, "NO-TABSTRIP", 0.0
    extent = max(bright) / scale

    if extent > C.EXTENT_OFFENSE:
        side, names, centres = "offense", C.OFF_TABS, C.OFF_X
    elif extent > C.EXTENT_DEFENSE:
        side, names, centres = "defense", C.DEF_TABS, C.DEF_X
    else:
        side, names, centres = "specialteams", C.ST_TABS, C.ST_X

    scores = []
    for cx in centres:
        px = int(cx * scale)
        half = int(38 * scale)
        scores.append(band_x(buf, w, h, bpr, bpp, px - half, px + half))
    i = max(range(len(scores)), key=lambda k: scores[k])
    ordered = sorted(scores, reverse=True)
    median = ordered[len(ordered) // 2]
    ratio = (scores[i] / median) if median else 0.0

    if ratio >= C.CONFIDENT:
        # Confirm this really IS a tab strip. Only runs once the cheap
        # brightness test already passed, so it costs ~1s per PLAY rather than
        # per poll - and it is the difference between navigating a real playcall
        # screen and grinding forever against a postgame screen that happened to
        # have a bright band in the right place.
        try:
            words = str(ocr.ocr(C.TABSTRIP,
                                 path=f"{C.SHOT_DIR}/tabtext.png")).upper()
        except Exception:
            words = ""
        if sum(1 for word in C.TAB_WORDS if word in words) < 2:
            return None, -1, "NOT-A-TABSTRIP", 0.0
    return side, i, names[i], ratio


def tab_names(side):
    return C.OFF_TABS if side == "offense" else C.DEF_TABS


def has_tab_words(region=None, path=None):
    try:
        txt = str(ocr.ocr(region or C.TABSTRIP,
                           path=path or f"{C.SHOT_DIR}/tt.png")).upper()
    except Exception:
        return False, ""
    return sum(1 for word in C.TAB_WORDS if word in txt) >= 2, txt


def action_bar():
    """The playcall action bar text, or "". Present on every playcall-family
    screen and blank during a live play, so it is a positive "we are in the
    playcall UI" signal that costs one crop."""
    try:
        return str(ocr.ocr(C.ACTIONBAR, path=f"{C.SHOT_DIR}/bar.png")).upper()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# THE LIVE CLOCK STRIP
# ---------------------------------------------------------------------------

def read_clocks():
    """Return (quarter, game_secs, play_secs); any element may be None.

    ⛔ Delegates to hud.py, which finds these by CONTENT rather than at fixed
    coordinates. Madden uses a different scoreboard layout per broadcast
    presentation and this event cycles through them like an NFL season, so a
    fixed crop reads bare grass in half of them. See hud.py's docstring.

    ⭐ The GAME clock runs down while the PLAY clock does (measured: 1:07 -> 1:06
    as the play clock went :26 -> :20). That is the entire basis for chewing.
    """
    import hud
    h = hud.read_clocks_fast()
    return h["quarter"], h["game"], h["play"]


def read_hud_full():
    """Everything on the HUD, from one capture, at full recall."""
    import hud
    return hud.read_hud()


def read_is_overtime():
    """True only if the HUD shows OT where the QUARTER belongs.

    ⛔ Not a text search. See hud.parse_hud - a loose scan matched a fragment on
    the special-teams screen and declared overtime during a 34-5 win, which
    silently disabled clock chewing for the rest of that game.
    """
    import hud
    try:
        return bool(hud.read_clocks_fast().get("overtime"))
    except Exception:
        return False


def read_score():
    """Return (theirs, ours, theirs_inferred) or None.

    ⛔ The opponent is on the LEFT of the bar and we are on the RIGHT in every
    layout seen, and each score is paired with ITS OWN team code rather than a
    fixed x - which side of the clock they sit on is exactly what changes
    between presentations.

    ``theirs_inferred`` is kept in the tuple for the caller's sake but is now
    always False: the zero-inference hack ("team code but no digit means 0") is
    gone, because the reader pairs a real token with each code and simply
    returns None when it cannot. A dropped read is now honestly unreadable
    rather than silently reported as a shut-out.
    """
    import hud
    h = hud.read_hud()
    if h["theirs"] is None or h["ours"] is None:
        return None
    return h["theirs"], h["ours"], False


# ---------------------------------------------------------------------------
# THE TIER BADGE
# ---------------------------------------------------------------------------

def read_tier_badge():
    """"ARCADE", "COMP", or None (the difficulty badge).

    ⭐ Reads the FULL SCREEN text, not a narrow crop. On the old build a 270x60
    crop at the badge's apparent position missed it on 6 of 6 attempts while the
    same badge showed up in full-screen OCR about half the time. The badge was
    real; the crop was wrong.

    ⛔ It only renders during a LIVE PLAY - never on the playcall screen, in
    menus, or in cut scenes - so None is the normal answer, not a failure. Call
    it repeatedly until it answers.

    ⛔ NEVER act on a single read: it has misread before. The badge is only
    logged, never acted on.
    """
    try:
        t = screen_text()
    except Exception:
        return None
    if "ARCADE" in t:
        return "ARCADE"
    # "COMP" only as its own word - it is a substring of COMPLETE, COMPETITION.
    if re.search(r"\bCOMP\b", t):
        return "COMP"
    return None


# ---------------------------------------------------------------------------
# TEMPO / COACH ADJUSTMENTS
# ---------------------------------------------------------------------------

def tempo_value():
    """Read the TEMPO value, defeating STALE frames.

    ⛔ Shot twice and use the second. A stale frame once read NORMAL after the
    value had already moved to CHEW, so the code stepped once more - onto NO
    HUDDLE, the exact opposite of what we want - then read the stale CHEW frame
    and declared success.
    """
    try:
        pad.shot(C.TEMPO_VALUE, f"{C.SHOT_DIR}/_tempo.png")   # discard as stale
        time.sleep(0.15)
        return str(ocr.ocr(C.TEMPO_VALUE,
                            path=f"{C.SHOT_DIR}/_tempo.png")).upper()
    except Exception:
        return ""


def tempo_label():
    try:
        return str(ocr.ocr(C.TEMPO_LABEL,
                            path=f"{C.SHOT_DIR}/_tlabel.png")).upper()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# CONSOLE REACHABILITY
# ---------------------------------------------------------------------------

# ⛔⛔ MARKERS THAT MEAN WE ARE LOOKING AT THE MAC, NOT THE PS5.
# After a chiaki restart the stream can end up behind other
# windows, and the daemon captures the WHOLE DISPLAY - so the loop was reading
# the macOS desktop and Terminal while still pressing buttons. The keycodes it
# sends include RETURN and BACKSPACE, which in a terminal are not harmless.
#
# This is the one failure where "keep trying" is the wrong instinct: every press
# goes somewhere real. Stop instead.
MAC_MARKERS = ("TERMINAL", "FINDER", "MACINTOSH HD", "AIRDROP",
               "SHELL | EDIT", "SYSTEM SETTINGS", "SPOTLIGHT",
               "DANGEROUSLY-SKIP-PERMISSIONS", "BYPASS PERMISSIONS")


def looks_like_mac(txt):
    """True if this capture is the Mac's own desktop rather than the stream."""
    up = str(txt).upper()
    return sum(1 for m in MAC_MARKERS if m in up) >= 2


def console_up(host=None):
    """Is the PS5 actually reachable?

    ⛔ PING FIRST, EVERY TIME. A frozen stream frame looks exactly like a
    loading screen: the console once dropped off the network and the loop logged
    "loading/transition -> waiting" 143 times because nothing checked
    reachability.
    """
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-t", "2", host or C.PS5_HOST],
            capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False
