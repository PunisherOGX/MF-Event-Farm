#!/usr/bin/env python3
"""Controller input + screen capture, through the local pad daemon.

Every press and every capture goes over localhost HTTP to mini-pad/padd.py,
which runs in the logged-in GUI session and holds the Screen Recording and
Accessibility grants. This process never needs either grant itself.

Two rules baked in:
  1. chiaki must be re-activated immediately BEFORE each press, or it silently
     stops forwarding keys (the daemon does this on every /press).
  2. chiaki stops presenting frames when it is not frontmost, so every capture
     is preceded by a focus, and the daemon captures twice and keeps the second.

Usage:
  pad.py press right right cross
  pad.py shot full out.png          # or x,y,w,h
  pad.py restart                    # kill + reconnect chiaki
"""
import json
import os
import subprocess
import sys
import threading
import time

PAD_URL = "http://127.0.0.1:8773"
_TOKEN = None


def _token():
    global _TOKEN
    if _TOKEN is None:
        with open(os.path.expanduser(os.environ.get("MUT_PAD_TOKEN_FILE",
                                                    "~/mut-pad/token"))) as fh:
            _TOKEN = fh.read().strip()
    return _TOKEN


# ONE persistent keep-alive connection to the daemon, not one per call: a fresh
# TCP connection per press/capture piles up TIME_WAIT entries on the port until
# connects start silently dropping. Any failure closes the socket and the call
# is retried once on a fresh one.
_CONN = None
_CONN_LOCK = threading.Lock()


def _conn_reset():
    global _CONN
    if _CONN is not None:
        try:
            _CONN.close()
        except Exception:
            pass
    _CONN = None


def _connect(host, timeout, tries=12):
    """Open the persistent connection: short connect timeout, several tries
    (each retry draws a fresh ephemeral port)."""
    import http.client
    last = None
    for _ in range(tries):
        c = http.client.HTTPConnection(host, timeout=1.5)
        try:
            c.connect()
            c.sock.settimeout(timeout)
            return c
        except Exception as e:
            last = e
            try:
                c.close()
            except Exception:
                pass
    raise last


def _http(path, body=None, timeout=60, raw=False):
    """One localhost call to the pad daemon, over the persistent connection."""
    global _CONN
    data = json.dumps(body).encode() if body is not None else None
    headers = {"X-Pad-Token": _token()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    host = PAD_URL.split("://", 1)[1]
    with _CONN_LOCK:
        for attempt in (1, 2):
            try:
                if _CONN is None:
                    _CONN = _connect(host, timeout)
                else:
                    _CONN.sock and _CONN.sock.settimeout(timeout)
                _CONN.request("POST" if data is not None else "GET", path,
                              body=data, headers=headers)
                r = _CONN.getresponse()
                payload = r.read()
                if r.getheader("Connection", "").lower() == "close":
                    _conn_reset()
                break
            except Exception:
                _conn_reset()
                if attempt == 2:
                    raise
    if raw:
        return payload
    return json.loads(payload) if payload else {}


def _remote_post(path, body, timeout=60):
    try:
        return _http(path, body, timeout)
    except Exception as e:
        return {"error": str(e)}


def _remote_shot(region, out, display=1, timeout=60):
    q = "name=full" if not region else "region=%d,%d,%d,%d" % tuple(region)
    data = _http("/shot?%s&display=%d" % (q, display), timeout=timeout, raw=True)
    if not data.startswith(b"\x89PNG"):
        raise RuntimeError("daemon capture failed: %s"
                           % (data[:200].decode(errors="replace") or "empty"))
    with open(out, "wb") as fh:
        fh.write(data)
    return out


# chiaki-ng "Keyboard as controller" map -> macOS virtual keycodes. Must match
# mini-pad/padd.py. Only the names matter here (config.py validates against them).
KEYS = {
    "up": 126, "down": 125, "left": 123, "right": 124,
    "cross": 36,         # Return
    "moon": 51,          # Backspace  - circle / back
    "pyramid": 8,        # C          - triangle
    "box": 42,           # backslash  - square
    "l1": 19, "r1": 20,  # 2 / 3
    "l2": 18, "r2": 21,  # 1 / 4
    "options": 31,       # O
    "ps": 53,            # Esc
    # lstick_up is F13 (105): chiaki's default "Ins" does not exist on a Mac
    # keyboard, so Left Stick Up must be rebound to F13 in chiaki's key settings.
    "lstick_up": 105,    # F13  - REBOUND
    "lstick_down": 117,  # Del
    "lstick_left": 33,   # [
    "lstick_right": 30,  # ]
    "rstick_up": 116,    # PgUp
    "rstick_down": 121,  # PgDown
    "rstick_left": 27,   # -
    "rstick_right": 24,  # =
    "l3": 23,            # 5
    "r3": 22,            # 6    - opens COACH ADJUSTMENTS (tempo)
}


def focus():
    _remote_post("/focus", {})


def fullscreen(on=True):
    """Every crop assumes the stream fills the display."""
    return _remote_post("/fullscreen", {"on": on}, timeout=90)


CHIAKI_APP = os.environ.get("MUT_CHIAKI_APP", "/Applications/chiaki-ng.app")

# MACHINE-SPECIFIC: where the console's tile sits in chiaki's host list, in
# display points. On a 1920x1080-point display with a single host it is
# ~(300, 175). Set MUT_CHIAKI_HOST_TILE="x,y" after checking a screenshot of
# the host list.
HOST_TILE = tuple(int(v) for v in
                  os.environ.get("MUT_CHIAKI_HOST_TILE", "300,175").split(","))


def release(timeout=5):
    """Lift every key. /release posts key-ups to whatever window has focus, so
    focus chiaki first - a release that lands in another app lifts nothing."""
    try:
        _remote_post("/focus", {}, timeout=timeout)
        time.sleep(0.15)
    except Exception:
        pass
    return _remote_post("/release", {}, timeout=timeout)


def restart_stream(tile=HOST_TILE, tries=2):
    """Kill chiaki by PID and reconnect. The only known cure for a dead renderer.

    Safe and non-destructive: quitting chiaki releases the Remote Play session
    and touches nothing on the PS5; the game stays exactly where it was.
    TERM the PID, then KILL it (quit-while-fullscreen can survive a TERM).
    """
    for attempt in range(1, tries + 1):
        r = subprocess.run(["/usr/bin/pgrep", "-x", "chiaki"],
                           capture_output=True)
        for pid in r.stdout.decode().split():
            subprocess.run(["/bin/kill", pid], capture_output=True)
        time.sleep(3)
        r = subprocess.run(["/usr/bin/pgrep", "-x", "chiaki"],
                           capture_output=True)
        for pid in r.stdout.decode().split():
            subprocess.run(["/bin/kill", "-9", pid], capture_output=True)
            time.sleep(2)

        subprocess.run(["/usr/bin/open", "-a", CHIAKI_APP],
                       capture_output=True, timeout=20)
        time.sleep(10)
        try:
            fullscreen(True)
        except Exception:
            pass
        time.sleep(3)
        # Double-click the host tile. Connecting can need a second click when
        # the launcher is still populating its discovered-host list.
        for _ in range(2):
            click(*tile, 2)
            time.sleep(24)
            if not _at_launcher():
                # A key held when the stream died never got its key-up: lift
                # everything before handing back.
                try:
                    _remote_post("/release", {}, timeout=5)
                except Exception:
                    pass
                print(f"  stream restarted (attempt {attempt})")
                return True
        print(f"  restart attempt {attempt} did not connect")
    return False


def _at_launcher():
    """True when chiaki is showing its host list instead of the stream."""
    import tempfile
    out = os.path.join(tempfile.gettempdir(), "_pad_launcher.png")
    try:
        shot((900, 60, 700, 60), out)
    except Exception:
        return True
    try:
        import Quartz
        import Vision
        from Foundation import NSURL
        url = NSURL.fileURLWithPath_(out)
        src = Quartz.CGImageSourceCreateWithURL(url, None)
        img = Quartz.CGImageSourceCreateImageAtIndex(src, 0, None)
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(0)
        h = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(
            img, None)
        h.performRequests_error_([req], None)
        t = " ".join(r.topCandidates_(1)[0].string()
                     for r in (req.results() or [])).upper()
    except Exception:
        return False
    return "ADD MANUAL HOST" in t or "REFRESH PSN" in t


def press_many(names, hold=0.06, gap=0.12):
    """Send a whole button sequence in one daemon call (menu navigation)."""
    names = list(names)
    if not names:
        return
    return _remote_post("/press", {"buttons": names, "hold": hold, "gap": gap})


def press(name, hold=0.15, gap=None):
    """One button. The daemon re-activates chiaki right before the press.
    `gap` is the daemon's trailing sleep after the button (default 0.35s)."""
    body = {"buttons": [name], "hold": hold}
    if gap is not None:
        body["gap"] = gap
    _remote_post("/press", body)


def click(x, y, clicks=1):
    _remote_post("/click", {"x": x, "y": y, "clicks": clicks})


def shot(region, out, display=1):
    """Capture `region` (x, y, w, h points, or None = full screen) to `out`.

    Focus first: chiaki stops presenting frames when it is not frontmost, and
    a capture without focus returns a stale screen. MUT_NO_CAPTURE_FOCUS=1
    skips it (expect stale frames if chiaki is not already frontmost).
    """
    if not os.environ.get("MUT_NO_CAPTURE_FOCUS"):
        focus()
        time.sleep(0.25)
    return _remote_shot(region, out, display)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "press":
        for b in sys.argv[2:]:
            if b not in KEYS:
                sys.exit(f"unknown button: {b}")
            press(b)
            time.sleep(0.35)
        print("sent:", " ".join(sys.argv[2:]))
    elif cmd == "shot":
        name, out = sys.argv[2], sys.argv[3]
        region = None if name == "full" else tuple(
            int(v) for v in name.split(","))
        shot(region, out)
        print("saved", out)
    elif cmd == "restart":
        ok = restart_stream()
        print("restarted" if ok else "RESTART FAILED")
        sys.exit(0 if ok else 1)
    else:
        print(__doc__)
