#!/usr/bin/env python
"""
MUT pad daemon — runs on the Mac mini inside the GUI (Aqua) session.

Owns the two things that only work from inside a logged-in GUI session:
  - controller input via Quartz.CGEventPost(kCGHIDEventTap, ...)
  - screen capture via /usr/sbin/screencapture

Listens on 127.0.0.1 only. Reached from the MacBook over SSH, e.g.
    ssh mini 'curl -s -H "X-Pad-Token: $(cat ~/mut-pad/token)" \
        -X POST 127.0.0.1:8773/press -d "{\"buttons\":[\"right\",\"cross\"]}"'

Why this exists: CGEventPost injects at the HID layer, which is machine-global.
Running it on the MacBook takes over the user's mouse and keyboard. Running it
here does not, because nobody is sitting at the mini.
"""

import hmac
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import Foundation
import Quartz

HOST = "127.0.0.1"
PORT = 8773                       # 8772 is reserved for the planned MUT market API
CHIAKI = "/Applications/chiaki-ng.app"
# Override with MUT_PAD_TOKEN_FILE. ⛔ The docs and the code MUST agree - an
# external review found SETUP.md telling users to write ~/pad-daemon/token while
# the code read ~/mut-pad/token, so the token was never found and (with the old
# fail-open auth) the daemon served every route unauthenticated.
TOKEN_FILE = os.path.expanduser(
    os.environ.get("MUT_PAD_TOKEN_FILE", "~/mut-pad/token"))
SHOT_DIR = "/tmp/mut-pad"

# chiaki-ng default "Keyboard as controller" map -> macOS virtual keycodes.
# Do not reorder or "clean up" — these are chiaki-ng's defaults, not ours.
KEYS = {
    "up": 126, "down": 125, "left": 123, "right": 124,
    "cross": 36,         # Return     - select / confirm / FIND THIS ITEM
    "moon": 51,          # Backspace  - circle / back
    "pyramid": 8,        # C          - triangle
    "box": 42,           # backslash  - square
    "l1": 19, "r1": 20,  # 2 / 3      - tab switching
    "l2": 18, "r2": 21,  # 1 / 4      - sort field / sort direction
    "options": 31,       # O
    "ps": 53,            # Esc
    # ⭐ ANALOG STICKS - verified live in the practice pause menu, Sep 3 2026.
    # The mapping is PERMANENT (the user's rule); only the BEHAVIOUR toggles.
    # Because these live in KEYS, /release can clear them - which is exactly the
    # latch hazard that wrecked the first attempt.
    #
    # ⛔ lstick_up is F13 (105), NOT 114. chiaki's DEFAULT for Left Stick Up is
    # "Ins", and macOS HAS NO INSERT KEY - 114 is kVK_Help and draws a "?"
    # overlay. Left Stick Up was REBOUND in chiaki Settings -> Keys to F13, which
    # persists as `keymap.left_stick_up = F13` in com.chiaki.Chiaki.plist.
    # If that binding is ever lost, restore it with:
    #     defaults write com.chiaki.Chiaki "keymap.left_stick_up" F13
    "lstick_up": 105,    # F13  - REBOUND (chiaki default "Ins" is unreachable)
    "lstick_down": 117,  # Del  - chiaki default, verified
    "lstick_left": 33,   # [    - chiaki default, verified
    "lstick_right": 30,  # ]    - chiaki default, verified
    "rstick_up": 116,    # PgUp
    "rstick_down": 121,  # PgDown
    "rstick_left": 27,   # -
    "rstick_right": 24,  # =
    "l3": 23,            # 5
    "r3": 22,            # 6    - proven: the chew-clock uses it every game
}

# Named crop regions, in POINTS (screencapture -R takes points, not pixels).
#
# Calibrated for the mini: 3840x2160 px = 1920x1080 points (2x HiDPI), with the
# stream filling the display exactly — true 16:9, no letterboxing.
#
# The old MacBook values are NOT a flat scale away from these. That display was
# 1512x982 points, so a 16:9 stream fit to width left ~66pt black bars top and
# bottom; content occupied y 65.75..916.25. Converting a MacBook point means
# going through stream-relative fractions:
#     fx = x / 1512                  fy = (y - 65.75) / 850.5
#     x_mini = fx * 1920             y_mini = fy * 1080
REGIONS = {
    "ah":    (499, 146, 241, 559),   # auction house strip — self-validating
    "bar":   (40, 1035, 620, 45),    # bottom action bar — tells you which screen
    "coins": (1200, 18, 650, 55),    # coin balance
    "grid":  (508, 94, 1346, 986),   # catalog cards + price row
    "full":  None,                   # whole display
}

# Serialize everything. Two overlapping press sequences would interleave
# keystrokes and put the console somewhere unintended.
LOCK = threading.Lock()


def load_token():
    """Read the shared secret. Returns None only if the file is unreadable.

    ⛔ A None here used to mean "serve everything unauthenticated" - see
    _authed(). It now means REFUSE TO START. This daemon can synthesise
    keystrokes and capture the screen, so an unauthenticated port is a genuine
    local-privilege problem, not a nuisance.
    """
    try:
        with open(TOKEN_FILE) as fh:
            tok = fh.read().strip()
        return tok or None
    except OSError:
        return None


TOKEN = load_token()

# ⛔⛔ FAIL CLOSED. Reported by an external review, Sep 6 2026, and it was right:
# with no token this daemon served EVERY route unauthenticated, so any local
# process could type into the machine and screenshot it. Refuse to start.
if not TOKEN:
    sys.stderr.write(
        "\nREFUSING TO START: no auth token.\n\n"
        f"  Create one at {TOKEN_FILE}:\n"
        f"    mkdir -p {os.path.dirname(TOKEN_FILE)}\n"
        f"    head -c 32 /dev/urandom | xxd -p > {TOKEN_FILE}\n"
        f"    chmod 600 {TOKEN_FILE}\n\n"
        "This daemon can send keystrokes and capture the screen. It will not\n"
        "run without authentication.\n\n")
    raise SystemExit(2)


def focus_chiaki():
    """Bring chiaki-ng to the front. `open -a` needs no Accessibility grant."""
    subprocess.run(["/usr/bin/open", "-a", CHIAKI], capture_output=True, timeout=10)


def chiaki_running():
    # -x (exact process name), NOT -f. With -f this matched any command line
    # containing "chiaki" — including the ssh/curl invocations driving this
    # daemon — so /health reported chiaki_running:true with chiaki long dead.
    r = subprocess.run(["/usr/bin/pgrep", "-x", "chiaki"], capture_output=True)
    return r.returncode == 0


def _osa(script):
    """Run one AppleScript line and return (rc, stdout, stderr).

    These run as a CHILD of this daemon on purpose. The daemon holds the
    Accessibility grant and the child inherits it. The identical script run
    over SSH fails with "osascript is not allowed assistive access" (-25211),
    because an SSH session is outside the Aqua session entirely.
    """
    r = subprocess.run(["/usr/bin/osascript", "-e", script],
                       capture_output=True, timeout=15)
    return (r.returncode,
            r.stdout.decode(errors="replace").strip(),
            r.stderr.decode(errors="replace").strip())


def chiaki_window_count():
    rc, out, err = _osa('tell application "System Events" '
                        'to tell process "chiaki" to get count of windows')
    if rc != 0:
        return 0, err
    try:
        return int(out), ""
    except ValueError:
        return 0, out


def set_fullscreen(want=True):
    """Put the chiaki stream window into macOS fullscreen.

    Every crop region assumes the stream fills the display exactly. A windowed
    chiaki leaves the menu bar and a title bar on screen, which shifts every
    region and silently corrupts every read — the numbers still parse, they are
    just measured off the wrong pixels.

    chiaki reports MORE THAN ONE AX window: one is a thin artifact, one is the
    real surface. Setting the attribute on the artifact does nothing and reports
    no error, so try every window and then read the state back rather than
    trusting an index.
    """
    val = "true" if want else "false"
    n, err = chiaki_window_count()
    if not n:
        return {"ok": False, "error": err or "no chiaki windows"}

    attempts = []
    for idx in range(1, n + 1):
        rc, _, e = _osa(f'tell application "System Events" to tell process "chiaki" '
                        f'to set value of attribute "AXFullScreen" of window {idx} to {val}')
        attempts.append({"window": idx, "rc": rc, "err": e[:160]})

    time.sleep(2.0)          # the fullscreen transition is animated

    states = []
    n2, _ = chiaki_window_count()
    for idx in range(1, (n2 or n) + 1):
        rc, out, e = _osa(f'tell application "System Events" to tell process "chiaki" '
                          f'to get value of attribute "AXFullScreen" of window {idx}')
        states.append({"window": idx,
                       "fullscreen": out if rc == 0 else f"err: {e[:80]}"})

    return {"ok": any(s["fullscreen"] == val for s in states),
            "windows": n, "attempts": attempts, "states": states}


def band_bright_fraction(region, size=(16, 8), bright_min=170):
    """Fraction of pixels in `region` brighter than `bright_min` on every
    channel. ⭐ The L1 badge at the left of the playcall tab strip (72,522,44,30)
    reads ~0.45 on the playcall page and 0.0 on live play AND the pause menu
    (Sep 18, 10 frames) - the one tell that is ONLY the playcall page."""
    did = Quartz.CGMainDisplayID()
    x, y, w, h = region
    img = Quartz.CGDisplayCreateImageForRect(did, Quartz.CGRectMake(x, y, w, h))
    if img is None:
        return None
    sw, sh = size
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, sw, sh, 8, sw * 4, cs,
                                       Quartz.kCGImageAlphaPremultipliedLast)
    if ctx is None:
        return None
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationLow)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, sw, sh), img)
    buf = bytes(Quartz.CGBitmapContextGetData(ctx).as_buffer(sw * sh * 4))
    n = sw * sh
    bright = sum(1 for i in range(0, len(buf), 4)
                 if buf[i] > bright_min and buf[i + 1] > bright_min and buf[i + 2] > bright_min)
    return bright / n


def band_dark_fraction(region, size=(64, 32), dark_max=70):
    """Fraction of pixels in `region` (points) darker than `dark_max` on every
    channel, sampled onto a tiny bitmap. ~1ms after a 0.03s capture.

    ⭐ THE PLAYCALL-PAGE TELL (Sep 18, measured on 12 frames): the play-card
    band (80,570,1760,250) reads >= 0.92 dark on the playcall page and the
    pause menu, <= 0.39 on every live-play frame. No OCR, no text, no font.
    """
    did = Quartz.CGMainDisplayID()
    x, y, w, h = region
    img = Quartz.CGDisplayCreateImageForRect(did, Quartz.CGRectMake(x, y, w, h))
    if img is None:
        return None
    sw, sh = size
    cs = Quartz.CGColorSpaceCreateDeviceRGB()
    ctx = Quartz.CGBitmapContextCreate(None, sw, sh, 8, sw * 4, cs,
                                       Quartz.kCGImageAlphaPremultipliedLast)
    if ctx is None:
        return None
    Quartz.CGContextSetInterpolationQuality(ctx, Quartz.kCGInterpolationLow)
    Quartz.CGContextDrawImage(ctx, Quartz.CGRectMake(0, 0, sw, sh), img)
    buf = bytes(Quartz.CGBitmapContextGetData(ctx).as_buffer(sw * sh * 4))
    n = sw * sh
    dark = sum(1 for i in range(0, len(buf), 4)
               if buf[i] < dark_max and buf[i + 1] < dark_max and buf[i + 2] < dark_max)
    return dark / n


def press(name, hold=0.06):
    code = KEYS[name]
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateKeyboardEvent(None, code, True))
    time.sleep(hold)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateKeyboardEvent(None, code, False))


def click(x, y, clicks=1, button="left"):
    """Move to (x, y) and click. Needed for the chiaki-ng launcher tile, which
    is not keyboard-reachable. Coordinates are in POINTS, like screencapture."""
    btn = Quartz.kCGMouseButtonLeft if button == "left" else Quartz.kCGMouseButtonRight
    down = Quartz.kCGEventLeftMouseDown if button == "left" else Quartz.kCGEventRightMouseDown
    up = Quartz.kCGEventLeftMouseUp if button == "left" else Quartz.kCGEventRightMouseUp

    move = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), btn)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
    time.sleep(0.12)

    for n in range(1, clicks + 1):
        for kind in (down, up):
            ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), btn)
            # Click state is what makes two clicks register as a double-click
            # rather than two unrelated single clicks.
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, n)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.04)
        time.sleep(0.06)


def _write_png(img, path):
    """Write a CGImage to disk as PNG."""
    url = Foundation.NSURL.fileURLWithPath_(path)
    dest = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
    if dest is None:
        raise RuntimeError("could not create PNG destination")
    Quartz.CGImageDestinationAddImage(dest, img, None)
    if not Quartz.CGImageDestinationFinalize(dest):
        raise RuntimeError("could not finalize PNG")


def capture(region=None, display=1, path=None, fresh=True):
    """Capture the screen (or a region) and return the file path.

    ⭐⭐ IN-PROCESS via CoreGraphics, NOT /usr/sbin/screencapture (Sep 6 2026).
    Measured on this mini:

        screencapture -R ...          1.90s   <- and FIXED, regardless of size
        CGDisplayCreateImageForRect   0.03s   <- 60x faster

    The 1.9s was pure per-invocation overhead - process spawn plus a TCC
    permission check on every single call - not pixel work: a 10x10 crop cost
    exactly as much as the whole screen. With ~8 captures per play that was
    ~15s per play of pure waiting, which is what pushed play-calling past the
    play clock and drew delay-of-game on every down.

    This daemon already holds the Screen Recording grant, so the in-process call
    needs no re-check. Output is byte-identical in size (verified: both produce
    1880x140 for the same 1900x140 point rect), so no OCR retuning was needed.

    chiaki-ng renders through hardware and can hand back a STALE frame, so the
    capture-twice-keep-the-second rule is KEPT - at 0.03s it now costs nothing.
    """
    os.makedirs(SHOT_DIR, exist_ok=True)
    path = path or os.path.join(SHOT_DIR, "shot.png")

    did = Quartz.CGMainDisplayID()
    if display and display > 1:
        err, ids, n = Quartz.CGGetActiveDisplayList(8, None, None)
        if not err and n >= display:
            did = ids[display - 1]

    attempts = 2 if fresh else 1
    img = None
    for i in range(attempts):
        if region:
            x, y, w, h = region
            img = Quartz.CGDisplayCreateImageForRect(
                did, Quartz.CGRectMake(x, y, w, h))
        else:
            img = Quartz.CGDisplayCreateImage(did)
        if img is None:
            raise RuntimeError("could not create image from display")
        if i + 1 < attempts:
            time.sleep(0.12)      # let the next frame land; was 0.4 for a 1.9s call

    _write_png(img, path)
    return path


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass  # stay quiet; launchd captures stdout/stderr to the log file

    # -- helpers -----------------------------------------------------------
    def _authed(self):
        # ⛔ No token => DENY. Never serve unauthenticated; startup already
        # refuses in that case, this is the second line of defence.
        if not TOKEN:
            return False
        return hmac.compare_digest(
            self.headers.get("X-Pad-Token", ""), TOKEN)

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _png(self, path):
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _region_from_query(self, q):
        if "region" in q:
            parts = [int(float(v)) for v in q["region"][0].split(",")]
            if len(parts) != 4:
                raise ValueError("region must be x,y,w,h")
            return tuple(parts)
        name = q.get("name", ["full"])[0]
        if name not in REGIONS:
            raise ValueError(f"unknown region name: {name}")
        return REGIONS[name]

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        if not self._authed():
            return self._json(403, {"error": "bad token"})
        u = urlparse(self.path)
        q = parse_qs(u.query)

        if u.path == "/health":
            return self._json(200, {
                "ok": True,
                "chiaki_running": chiaki_running(),
                "buttons": sorted(KEYS),
                "regions": {k: v for k, v in REGIONS.items()},
                "port": PORT,
            })

        if u.path == "/shot":
            try:
                region = self._region_from_query(q)
                display = int(q.get("display", ["1"])[0])
                with LOCK:
                    path = capture(region, display)
                return self._png(path)
            except Exception as e:
                return self._json(500, {"error": str(e)})

        return self._json(404, {"error": "no such route"})

    def do_POST(self):
        if not self._authed():
            return self._json(403, {"error": "bad token"})
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError as e:
            return self._json(400, {"error": f"bad json: {e}"})

        if u.path == "/key":
            # Send a RAW macOS virtual keycode. For probing chiaki-ng's actual
            # keyboard->controller map when a name in KEYS turns out to be wrong
            # for this build.
            try:
                code = int(body["code"])
            except (KeyError, TypeError, ValueError):
                return self._json(400, {"error": "need integer code"})
            hold = float(body.get("hold", 0.15))
            with LOCK:
                if body.get("focus", True):
                    focus_chiaki()
                    time.sleep(0.5)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                                   Quartz.CGEventCreateKeyboardEvent(None, code, True))
                time.sleep(hold)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                                   Quartz.CGEventCreateKeyboardEvent(None, code, False))
            return self._json(200, {"ok": True, "code": code})

        if u.path == "/combo":
            # Hold one button DOWN while tapping others — e.g. Madden's play-call
            # variant picker: hold triangle, tap down x3, release triangle.
            #
            # This must run daemon-side. Doing it from the client would put an
            # HTTP round trip between the key-down and each tap, and any jitter
            # there lands *inside* the hold window, which is precisely the timing
            # the game is reading.
            #
            # The key-up is in a finally: a key-down that never gets its key-up
            # leaves chiaki believing the button is held, and the game stops
            # responding to everything else. That is the exact state /release
            # exists to clean up, and we must not create it.
            hold_btn = body.get("hold")
            taps = body.get("taps") or []
            if hold_btn not in KEYS:
                return self._json(400, {"error": f"unknown hold button: {hold_btn}"})
            bad = [t for t in taps if t not in KEYS]
            if bad:
                return self._json(400, {"error": f"unknown tap button(s): {bad}"})

            pre = float(body.get("pre", 0.30))       # let the sub-menu appear
            tap_hold = float(body.get("tap_hold", 0.06))
            gap = float(body.get("gap", 0.18))
            post = float(body.get("post", 0.25))     # settle before releasing
            hcode = KEYS[hold_btn]

            with LOCK:
                if body.get("focus", True):
                    focus_chiaki()
                    time.sleep(0.5)
                Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                                   Quartz.CGEventCreateKeyboardEvent(None, hcode, True))
                try:
                    time.sleep(pre)
                    for t in taps:
                        tcode = KEYS[t]
                        Quartz.CGEventPost(
                            Quartz.kCGHIDEventTap,
                            Quartz.CGEventCreateKeyboardEvent(None, tcode, True))
                        time.sleep(tap_hold)
                        Quartz.CGEventPost(
                            Quartz.kCGHIDEventTap,
                            Quartz.CGEventCreateKeyboardEvent(None, tcode, False))
                        time.sleep(gap)
                    time.sleep(post)
                finally:
                    Quartz.CGEventPost(
                        Quartz.kCGHIDEventTap,
                        Quartz.CGEventCreateKeyboardEvent(None, hcode, False))
            return self._json(200, {"ok": True, "hold": hold_btn, "taps": taps})

        if u.path == "/sequence":
            # ONE request carrying a WHOLE timed macro, executed daemon-side.
            # (Sep 17) The client-side alternative is one HTTP call per press,
            # each ~2ms but spiking to 24ms - and a recorded macro's timing IS
            # the macro. Every event is {"t": seconds from start, "key": name,
            # "down": bool}; the daemon sleeps to each t on a monotonic clock
            # (drift does not accumulate) and posts it. Any key still down at
            # the end - or when anything raises - is released in the finally,
            # so a macro cannot leave chiaki believing a button is held.
            events = body.get("events") or []
            if not isinstance(events, list) or not events:
                return self._json(400, {"error": "need a non-empty events list"})
            plan = []
            for i, ev in enumerate(events):
                try:
                    key = ev["key"]; t = float(ev["t"]); down = bool(ev["down"])
                except (KeyError, TypeError, ValueError):
                    return self._json(400, {"error": f"bad event #{i}: {ev!r}"})
                if key not in KEYS:
                    return self._json(400, {"error": f"unknown key in event #{i}: {key}"})
                if t < 0:
                    return self._json(400, {"error": f"negative t in event #{i}"})
                plan.append((t, KEYS[key], down, key))
            plan.sort(key=lambda e: e[0])
            if plan[-1][0] > float(body.get("max_seconds", 30.0)):
                return self._json(400, {"error": "sequence longer than max_seconds"})

            held = set()
            late = 0.0
            with LOCK:
                if body.get("focus", True):
                    focus_chiaki()
                    time.sleep(0.5)
                start = time.monotonic()
                try:
                    for t, code, down, _name in plan:
                        delay = start + t - time.monotonic()
                        if delay > 0:
                            time.sleep(delay)
                        else:
                            late = max(late, -delay)
                        Quartz.CGEventPost(
                            Quartz.kCGHIDEventTap,
                            Quartz.CGEventCreateKeyboardEvent(None, code, down))
                        (held.add if down else held.discard)(code)
                finally:
                    for code in held:
                        Quartz.CGEventPost(
                            Quartz.kCGHIDEventTap,
                            Quartz.CGEventCreateKeyboardEvent(None, code, False))
                elapsed = time.monotonic() - start
            return self._json(200, {"ok": True, "events": len(plan),
                                    "elapsed": round(elapsed, 3),
                                    "max_late_ms": round(late * 1000, 1),
                                    "auto_released": len(held)})

        if u.path == "/hold_watch":
            # HOLD keys CONTINUOUSLY and let go the instant the screen says the
            # play is over - decided HERE, from a 0.03s in-process capture every
            # `interval`, never from a client round trip.
            # (Sep 18) The client-side version held in 0.5s chunks with ~0.5s
            # screen checks between them. Two consequences the user saw from
            # the couch: R2 delivered as half-second stabs never sprinted, and
            # the tail of a chunk landed on the playcall grid and scrolled the
            # favourites rows, so cross then picked the wrong play. This holds
            # every key for the whole run and releases within one interval of
            # the playcall page appearing - a dark, uniform play-card band.
            keys = body.get("keys") or []
            if isinstance(keys, str):
                keys = keys.split()
            unknown = [k for k in keys if k not in KEYS]
            if unknown or not keys:
                return self._json(400, {"error": f"bad keys: {unknown or keys}"})
            max_s = float(body.get("max_seconds", 3.0))
            interval = float(body.get("interval", 0.05))
            min_hold = float(body.get("min_hold", 0.15))
            region = tuple(body.get("region") or (80, 570, 1760, 250))
            threshold = float(body.get("dark_fraction", 0.70))
            # ⛔ (Sep 18) "band dark" alone fired DURING live plays - the pass
            # camera tilts up and the band goes dark before the catch; 9 of 24
            # in-game holds were cut under 1.2s and R2 never sprinted. Require
            # a second, playcall-only tell: the L1 badge lit.
            confirm_region = body.get("confirm_region")
            confirm_bright = float(body.get("confirm_bright", 0.25))
            # ⭐ RE-PRESS some keys during the hold. (Sep 18) R2 held from before
            # the catch never sprinted - Madden wants a fresh press once control
            # passes to the receiver. The stick stays down; R2 gets a 30ms lift
            # every `repress_every` so an edge lands after the catch.
            repress = body.get("repress") or []
            if isinstance(repress, str):
                repress = repress.split()
            bad = [k for k in repress if k not in keys]
            if bad:
                return self._json(400, {"error": f"repress keys not held: {bad}"})
            repress_codes = [KEYS[k] for k in repress]
            repress_every = float(body.get("repress_every", 0.4))
            next_repress = repress_every
            codes = [KEYS[k] for k in keys]
            reason, samples, last = "max", 0, None
            with LOCK:
                if body.get("focus", True):
                    focus_chiaki()
                    time.sleep(0.5)
                start = time.monotonic()
                try:
                    for code in codes:
                        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                                           Quartz.CGEventCreateKeyboardEvent(None, code, True))
                    while True:
                        now = time.monotonic() - start
                        if now >= max_s:
                            break
                        if now >= min_hold:
                            try:
                                last = band_dark_fraction(region)
                            except Exception:
                                last = None
                            samples += 1
                            if last is not None and last >= threshold:
                                ok = True
                                if confirm_region:
                                    try:
                                        cb = band_bright_fraction(tuple(confirm_region))
                                    except Exception:
                                        cb = None
                                    ok = cb is not None and cb >= confirm_bright
                                if ok:
                                    reason = "screen"
                                    break
                        if repress_codes and now >= next_repress:
                            for code in repress_codes:
                                Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                                                   Quartz.CGEventCreateKeyboardEvent(None, code, False))
                            time.sleep(0.03)
                            for code in repress_codes:
                                Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                                                   Quartz.CGEventCreateKeyboardEvent(None, code, True))
                            next_repress += repress_every
                        time.sleep(interval)
                finally:
                    # ⛔ Every key up, in reverse, whatever happened above.
                    for code in reversed(codes):
                        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                                           Quartz.CGEventCreateKeyboardEvent(None, code, False))
                held = time.monotonic() - start
            return self._json(200, {"ok": True, "keys": keys, "held": round(held, 3),
                                    "reason": reason, "samples": samples,
                                    "dark": None if last is None else round(last, 2)})

        if u.path == "/release":
            # Post key-up for every mapped key. If a key-down ever lands without
            # its key-up, chiaki thinks that button is held and the game stops
            # responding to anything else. This clears that state.
            # ⛔⛔ FOCUS FIRST. (Sep 18) chiaki only sees key events while it is
            # frontmost. A /release posted while something else had focus
            # cleared nothing: the game sat on "[RELEASE] SELECT STUNT" through
            # 16 delay-of-game flags (1st & 42) until a release WITH focus. This
            # was the only endpoint that skipped focus_chiaki().
            with LOCK:
                if body.get("focus", True):
                    focus_chiaki()
                    time.sleep(0.5)     # same settle as /press - 0.15s lost the race
                for code in set(KEYS.values()):
                    Quartz.CGEventPost(
                        Quartz.kCGHIDEventTap,
                        Quartz.CGEventCreateKeyboardEvent(None, code, False))
                    time.sleep(0.02)
            return self._json(200, {"ok": True, "released": len(set(KEYS.values()))})

        if u.path == "/focus":
            with LOCK:
                focus_chiaki()
            return self._json(200, {"ok": True})

        if u.path == "/fullscreen":
            # Must be fullscreen before ANY crop is trusted — see set_fullscreen.
            want = bool(body.get("on", True))
            with LOCK:
                focus_chiaki()
                time.sleep(1.0)
                result = set_fullscreen(want)
            return self._json(200 if result.get("ok") else 500, result)

        if u.path == "/click":
            try:
                x = float(body["x"])
                y = float(body["y"])
            except (KeyError, TypeError, ValueError):
                return self._json(400, {"error": "need numeric x and y"})
            clicks = int(body.get("clicks", 1))
            with LOCK:
                if body.get("focus", True):
                    focus_chiaki()
                    time.sleep(0.5)
                click(x, y, clicks=clicks, button=body.get("button", "left"))
            return self._json(200, {"ok": True, "clicked": [x, y], "clicks": clicks})

        if u.path == "/launch":
            """Open chiaki-ng and optionally double-click the console tile."""
            with LOCK:
                focus_chiaki()
                time.sleep(float(body.get("wait", 3.0)))
                tile = body.get("tile")
                if tile:
                    click(float(tile[0]), float(tile[1]), clicks=2)
            return self._json(200, {"ok": True, "running": chiaki_running()})

        if u.path == "/press":
            buttons = body.get("buttons") or []
            if isinstance(buttons, str):
                buttons = buttons.split()
            unknown = [b for b in buttons if b not in KEYS]
            if unknown:
                return self._json(400, {"error": f"unknown buttons: {unknown}"})

            hold = float(body.get("hold", 0.06))
            gap = float(body.get("gap", 0.35))
            settle = float(body.get("settle", 1.2))
            # Focus MUST default ON. chiaki-ng's keyboard-as-controller stops
            # forwarding keys after a while unless the app is re-activated before
            # each press — being frontmost per lsappinfo is NOT sufficient, and
            # neither is clicking inside the stream. Verified the hard way:
            # turning this off silently killed all keyboard input mid-session
            # while mouse input kept working.
            do_focus = bool(body.get("focus", True))

            with LOCK:
                if do_focus:
                    focus_chiaki()
                    time.sleep(0.5)
                for b in buttons:
                    press(b, hold=hold)
                    time.sleep(gap)
                result = {"ok": True, "sent": buttons}
                # Optional: return a crop after the presses land, so the caller
                # can confirm which screen it ended up on in the same round trip.
                verify = body.get("verify")
                if verify:
                    time.sleep(settle)
                    try:
                        region = REGIONS.get(verify)
                        if verify not in REGIONS:
                            raise ValueError(f"unknown region: {verify}")
                        path = capture(region, int(body.get("display", 1)),
                                       os.path.join(SHOT_DIR, f"verify_{verify}.png"))
                        result["verify_path"] = path
                    except Exception as e:
                        result["verify_error"] = str(e)
            return self._json(200, result)

        return self._json(404, {"error": "no such route"})


def main():
    os.makedirs(SHOT_DIR, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"mut-pad listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
