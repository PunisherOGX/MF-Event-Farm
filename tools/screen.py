#!/usr/bin/env python3
"""Everything that READS the screen. No button ever gets pressed from here.

Look at the screen; do not reason about what the code probably does. Reading
is cheap - keep it that way and use it constantly.

⛔ NEVER shell out to `screencapture`: ~1.9s per call and frames that are
seconds out of date. The pad daemon captures in-process in ~0.03s.
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


def box_mean(buf, w, h, bpr, bpp, x0, x1, y0, y1):
    """Mean luminance of a RECTANGLE, sampled every 2nd pixel.

    Sample the BUTTON, not the row: a full-width band averages a menu
    highlight away (a button is ~430px of a 1920px display).
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

def ocr_boxes_image(img, scale=2.0):
    """OCR an in-memory CGImage -> [{text, x0, y0, x1, y1, cx, cy, fy, conf}]
    in pixels, top-left origin. Lets several sub-crops of ONE capture be
    recognised without decoding the PNG again for each (the HUD reader).

    ⛔ Vision's boundingBox is normalised with a BOTTOM-LEFT origin; the flip is
    done here so no caller has to remember it.
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
    """OCR boxes (as ocr_boxes_image) for an ALREADY-CAPTURED file.

    A decision that compares TEXT with BRIGHTNESS must read both from the SAME
    frame, so menu selection captures once and reads this file twice.
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

    Between-game screens have no clock pressure, so they are identified by
    their TEXT rather than by pixel geometry.
    """
    try:
        txt = str(ocr.ocr(None)).upper()
    except Exception:
        return ""
    # A stuck macOS banner ("App Background Activity ... Extensions.") can sit
    # over the stream and make a loading screen read as "unknown". Drop it.
    return _MAC_BANNER.sub("", txt)


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
        # Confirm this really IS a tab strip (a postgame screen can have a
        # bright band in the right place). Only runs once the cheap brightness
        # test passed, so it costs ~1s per play rather than per poll.
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

    Delegates to hud.py, which finds these by CONTENT rather than at fixed
    coordinates (Madden's scoreboard layout changes per presentation).
    The GAME clock runs down while the PLAY clock does: the basis for chewing.
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

    Not a text search: a loose scan false-positives (see hud.parse_hud).
    """
    import hud
    try:
        return bool(hud.read_clocks_fast().get("overtime"))
    except Exception:
        return False


def read_score():
    """Return (theirs, ours) or None. The opponent is on the LEFT of the bar
    and we are on the RIGHT; each score is paired with its own team code."""
    import hud
    h = hud.read_hud()
    if h["theirs"] is None or h["ours"] is None:
        return None
    return h["theirs"], h["ours"]


# ---------------------------------------------------------------------------
# THE TIER BADGE
# ---------------------------------------------------------------------------

def read_tier_badge():
    """"ARCADE", "COMP", or None (the difficulty badge), from full-screen OCR.

    It only renders during a LIVE PLAY, so None is the normal answer. Logged
    only, never acted on.
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

    Shot twice and use the second: a stale frame once read NORMAL after the
    value had already moved to CHEW, and one more step landed on NO HUDDLE.
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

# ⛔ MARKERS THAT MEAN WE ARE LOOKING AT THE MAC, NOT THE PS5. After a chiaki
# restart the stream can end up behind other windows; the daemon captures the
# whole display, and the keycodes it sends (RETURN, BACKSPACE) are not harmless
# in a terminal. The one failure where "keep trying" is wrong: stop instead.
MAC_MARKERS = ("TERMINAL", "FINDER", "MACINTOSH HD", "AIRDROP",
               "SHELL | EDIT", "SYSTEM SETTINGS", "SPOTLIGHT",
               "DANGEROUSLY-SKIP-PERMISSIONS", "BYPASS PERMISSIONS")


def looks_like_mac(txt):
    """True if this capture is the Mac's own desktop rather than the stream."""
    up = str(txt).upper()
    return sum(1 for m in MAC_MARKERS if m in up) >= 2


def console_up(host=None):
    """Is the PS5 actually reachable?

    A frozen stream frame looks exactly like a loading screen, so ping.
    """
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-t", "2", host or C.PS5_HOST],
            capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False
