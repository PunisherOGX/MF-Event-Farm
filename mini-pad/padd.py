#!/usr/bin/env python
"""
MUT pad daemon - runs inside the logged-in GUI (Aqua) session of the Mac that
shows the chiaki-ng stream.

Owns the two things that only work from inside a GUI session with the right
grants (Screen Recording + Accessibility):
  - controller input via Quartz.CGEventPost(kCGHIDEventTap, ...), i.e. chiaki's
    "keyboard as controller" keys
  - screen capture via CoreGraphics

Listens on 127.0.0.1 only and requires the token in ~/mut-pad/token, e.g.
    curl -s -H "X-Pad-Token: $(cat ~/mut-pad/token)" \
        -X POST 127.0.0.1:8773/press -d '{"buttons":["right","cross"]}'

CGEventPost injects at the HID layer, which is machine-global: while the farm
runs, this Mac's keyboard focus belongs to chiaki. Use a machine nobody is
sitting at.
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
PORT = 8773
CHIAKI = "/Applications/chiaki-ng.app"
# Override with MUT_PAD_TOKEN_FILE (tools/pad.py reads the same variable).
TOKEN_FILE = os.path.expanduser(
    os.environ.get("MUT_PAD_TOKEN_FILE", "~/mut-pad/token"))
SHOT_DIR = "/tmp/mut-pad"

# chiaki-ng default "Keyboard as controller" map -> macOS virtual keycodes.
# These are chiaki-ng's defaults (plus the F13 rebind below), not ours.
KEYS = {
    "up": 126, "down": 125, "left": 123, "right": 124,
    "cross": 36,         # Return     - select / confirm
    "moon": 51,          # Backspace  - circle / back
    "pyramid": 8,        # C          - triangle
    "box": 42,           # backslash  - square
    "l1": 19, "r1": 20,  # 2 / 3      - tab switching
    "l2": 18, "r2": 21,  # 1 / 4
    "options": 31,       # O
    "ps": 53,            # Esc
    # Analog sticks. Because these live in KEYS, /release can clear them.
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
    "r3": 22,            # 6    - opens COACH ADJUSTMENTS (tempo)
}

# Named crop regions, in POINTS. Callers pass region=x,y,w,h; "full" is the
# whole display.
REGIONS = {"full": None}

# Serialize everything. Two overlapping press sequences would interleave
# keystrokes and put the console somewhere unintended.
LOCK = threading.Lock()


def load_token():
    """Read the shared secret. None (unreadable/empty) means REFUSE TO START:
    this daemon can synthesise keystrokes and capture the screen."""
    try:
        with open(TOKEN_FILE) as fh:
            tok = fh.read().strip()
        return tok or None
    except OSError:
        return None


TOKEN = load_token()

# FAIL CLOSED: never serve unauthenticated.
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
    # -x (exact process name), NOT -f: -f matches any command line that
    # merely contains "chiaki".
    r = subprocess.run(["/usr/bin/pgrep", "-x", "chiaki"], capture_output=True)
    return r.returncode == 0


def _osa(script):
    """Run one AppleScript line and return (rc, stdout, stderr).

    These run as a CHILD of this daemon on purpose: the daemon holds the
    Accessibility grant and the child inherits it.
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
    channel. The L1 badge at the left of the playcall tab strip (72,522,44,30)
    reads ~0.45 on the playcall page and 0.0 on live play AND the pause menu -
    the one tell that is ONLY the playcall page."""
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

    The playcall-page tell (measured): the play-card
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
    is not keyboard-reachable. Coordinates are in POINTS."""
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

    In-process via CoreGraphics (~0.03s), NOT /usr/sbin/screencapture (~1.9s
    fixed per call, which pushed play-calling past the play clock).

    chiaki-ng renders through hardware and can hand back a STALE frame, so it
    captures twice and keeps the second.
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

        if u.path == "/hold_watch":
            # HOLD keys CONTINUOUSLY and let go the instant the screen says the
            # play is over - decided HERE, from a 0.03s in-process capture every
            # `interval`, never from a client round trip.
            # A client-side chunked hold delivered R2 as stabs that never
            # sprinted, and a chunk's tail landed on the playcall grid. This
            # holds every key for the whole run and releases within one
            # interval of the playcall page appearing.
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
            # "Band dark" alone also fires during live plays (camera tilts);
            # require a second, playcall-only tell: the L1 badge lit.
            confirm_region = body.get("confirm_region")
            confirm_bright = float(body.get("confirm_bright", 0.25))
            # Optional RE-PRESS of some keys during the hold: each gets a 30ms
            # lift every `repress_every` (the farm sends none).
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
            # FOCUS FIRST: chiaki only sees key events while it is frontmost.
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

        if u.path == "/press":
            buttons = body.get("buttons") or []
            if isinstance(buttons, str):
                buttons = buttons.split()
            unknown = [b for b in buttons if b not in KEYS]
            if unknown:
                return self._json(400, {"error": f"unknown buttons: {unknown}"})

            hold = float(body.get("hold", 0.06))
            gap = float(body.get("gap", 0.35))
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
            return self._json(200, {"ok": True, "sent": buttons})

        return self._json(404, {"error": "no such route"})


def main():
    os.makedirs(SHOT_DIR, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"mut-pad listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
