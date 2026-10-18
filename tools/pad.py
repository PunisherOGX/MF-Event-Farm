#!/usr/bin/env python
"""Controller input + screen capture for chiaki-ng on the MacBook.

Two hard-won rules baked in (see workflow §14.1-14.2):
  1. chiaki must be re-activated immediately BEFORE each press, or it silently
     stops forwarding keys while mouse input keeps working.
  2. screencapture returns STALE frames (chiaki renders through hardware), so
     every capture is taken twice and the second is kept.

Usage:
  pad.py press right right cross
  pad.py shot ah out.png          # ah | bar | coins | grid | full  or x,y,w,h
  pad.py click 529 231 2
"""
import base64
import json
import os
import shlex
import subprocess
import threading
import sys
import time

import Quartz

# ---------------------------------------------------------------------------
# REMOTE MODE — drive chiaki on the Mac mini instead of this machine.
#
#   export MUT_PAD_REMOTE=mini        # an ssh host alias from ~/.ssh/config
#
# Why: CGEventPost injects at the HID layer, which is machine-global, so local
# mode takes over the player's mouse and keyboard for the whole run. In remote
# mode every press and capture happens on the mini and this machine stays free.
#
# step.py needs NO changes — it only calls focus/press/click/shot, and each of
# those forwards over SSH when MUT_PAD_REMOTE is set. Unset, behaviour here is
# byte-identical to the local path that ran Sessions 4-7.
# ---------------------------------------------------------------------------
REMOTE = os.environ.get("MUT_PAD_REMOTE") or None
PAD_URL = "http://127.0.0.1:8773"

# ---------------------------------------------------------------------------
# ON-MINI MODE —  export MUT_PAD_REMOTE=local
#
# When Claude Code itself runs ON the mini there is no reason to tunnel through
# ssh back to the same machine. Talk to the pad daemon straight over localhost
# HTTP instead: a round trip drops from ~300-500ms (ssh + curl + a process
# spawn, each way) to single-digit milliseconds.
#
# It also sidesteps TCC entirely. Screen Recording and Accessibility are granted
# per-app, and the daemon (a LaunchAgent in the Aqua session) already holds
# both. Routing through it means Terminal never needs its own grants — which is
# the exact wall that `osascript` hit over ssh: "not allowed assistive access".
# ---------------------------------------------------------------------------
LOCAL_DAEMON = REMOTE in ("local", "localhost", "127.0.0.1")
_TOKEN = None


def _token():
    global _TOKEN
    if _TOKEN is None:
        with open(os.path.expanduser(os.environ.get("MUT_PAD_TOKEN_FILE", "~/mut-pad/token"))) as fh:
            _TOKEN = fh.read().strip()
    return _TOKEN


# ⛔⛔ ONE PERSISTENT CONNECTION TO THE DAEMON, NOT ONE PER CALL.
# urllib opened a fresh TCP connection for every press and every capture -
# thousands per game - and each one left a TIME_WAIT entry on 127.0.0.1:8773.
# The mini's kernel (50 days up) stopped reaping them: 10,022 sat there for
# minutes without expiring, a new connect() kept landing on an ephemeral port
# still in TIME_WAIT, and the SYN was dropped silently. That looked exactly
# like a hung daemon ("urlopen error timed out" on the first /shot of a run),
# but the daemon was idle in poll() the whole time. The daemon already speaks
# HTTP/1.1 with Content-Length on every reply, so keep-alive costs nothing:
# one socket for the whole run, zero churn. Any failure closes the socket and
# the call is retried ONCE on a fresh one.
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
    """Open the persistent connection. ⛔ Short connect timeout, several
    tries: while the kernel holds thousands of un-reaped TIME_WAIT entries
    on this port, a connect() that draws one of those ephemeral ports hangs
    until it times out. Every retry draws a fresh port, so a few 1.5s tries
    get through where one 60s wait would not."""
    import http.client
    last = None
    for i in range(tries):
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
    """One localhost call to the pad daemon, over a persistent connection.
    Used only in on-mini mode."""
    import http.client
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
                # A dropped/half-closed keep-alive shows up as BadStatusLine,
                # RemoteDisconnected, ConnectionReset or a timeout. Fresh
                # socket, one retry, then let the caller see it.
                _conn_reset()
                if attempt == 2:
                    raise
    if raw:
        return payload
    return json.loads(payload) if payload else {}


# One multiplexed SSH connection for the whole run. Without this every call pays
# a fresh TCP + crypto handshake (~1.4s measured over Tailscale), which is what
# made the Aug 6 mini attempt feel unusable. With it, later calls reuse the open
# channel and cost roughly one round trip.
_SSH_CTL = "/tmp/mut-pad-ssh-%r@%h:%p"
_SSH_OPTS = ["-o", "ConnectTimeout=15",
             "-o", "ControlMaster=auto",
             "-o", "ControlPath=" + _SSH_CTL,
             "-o", "ControlPersist=600"]


def _ssh(shell_cmd, timeout=90):
    """Run one command on the mini. Returns CompletedProcess (stdout is bytes)."""
    return subprocess.run(["/usr/bin/ssh"] + _SSH_OPTS + [REMOTE, shell_cmd],
                          capture_output=True, timeout=timeout)


def _remote_post(path, body, timeout=60):
    if LOCAL_DAEMON:
        try:
            return _http(path, body, timeout)
        except Exception as e:
            return {"error": str(e)}
    cmd = ('T=$(cat ~/mut-pad/token); curl -s --max-time %d -X POST '
           '-H "X-Pad-Token: $T" %s%s -d %s'
           % (timeout, PAD_URL, path, shlex.quote(json.dumps(body))))
    r = _ssh(cmd, timeout=timeout + 30)
    out = r.stdout.decode(errors="replace").strip()
    try:
        return json.loads(out) if out else {}
    except json.JSONDecodeError:
        return {"error": out or r.stderr.decode(errors="replace").strip()}


def _remote_shot(region, out, display=1, timeout=60):
    """Fetch a capture from the mini. base64 so one SSH round trip carries it."""
    q = "name=full" if not region else "region=%d,%d,%d,%d" % tuple(region)
    if LOCAL_DAEMON:
        # Straight over the loopback - no ssh, no base64 round trip. The daemon
        # still does the capture-twice-keep-the-second dance on its side (§7.6).
        data = _http("/shot?%s&display=%d" % (q, display), timeout=timeout, raw=True)
        if not data.startswith(b"\x89PNG"):
            raise RuntimeError("local daemon capture failed: %s"
                               % (data[:200].decode(errors="replace") or "empty"))
        with open(out, "wb") as fh:
            fh.write(data)
        return out
    cmd = ('T=$(cat ~/mut-pad/token); curl -s --max-time %d -H "X-Pad-Token: $T" '
           "'%s/shot?%s&display=%d' | base64" % (timeout, PAD_URL, q, display))
    r = _ssh(cmd, timeout=timeout + 30)
    data = base64.b64decode(r.stdout or b"")
    if not data.startswith(b"\x89PNG"):
        raise RuntimeError("remote capture failed: %s"
                           % (data[:200].decode(errors="replace") or "empty response"))
    with open(out, "wb") as fh:
        fh.write(data)
    return out

# chiaki-ng default "Keyboard as controller" map -> macOS virtual keycodes
KEYS = {
    "up": 126, "down": 125, "left": 123, "right": 124,
    "cross": 36,         # Return     - select / confirm / FIND THIS ITEM
    "moon": 51,          # Backspace  - circle / back
    "pyramid": 8,        # C
    "box": 42,           # backslash
    "l1": 19, "r1": 20,  # 2 / 3      - tab switching
    "l2": 18, "r2": 21,  # 1 / 4      - sort field / direction
    "options": 31,       # O
    "ps": 53,            # Esc
    # ⭐ ANALOG STICKS - verified live in the practice pause menu.
    # The mapping is PERMANENT (the player's rule); only the BEHAVIOUR toggles.
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
    "r3": 22,            # 6    - proven: the chew-clock uses it every game        - PS5 Control Center
}

# MacBook built-in display: 3024x1964 px = 1512x982 points. -R takes POINTS.
REGIONS = {
    "ah":    (393, 181, 190, 440),
    "bar":   (30, 893, 500, 35),
    "coins": (950, 85, 500, 55),
    "grid":  (400, 140, 1060, 800),
    "full":  None,
}

# Mac mini: 3840x2160 px = 1920x1080 POINTS (2x HiDPI) and the 16:9 stream fills
# it exactly — NO letterbox, unlike the MacBook's ~66pt bars. So these are not a
# scale of the values above; they are the Aug 6 mini calibration, which still
# holds because -R takes points and the point space is unchanged.
REMOTE_REGIONS = {
    # `ah` starts 10pt higher than the Aug 6 calibration. At y=146 the top of
    # the `N ITEMS FOUND` glyphs was clipped and Vision returned garbage for it,
    # which cost strip() its OVR anchor (see step.py). Bottom edge is unchanged
    # at 705, so nothing else about the crop moves.
    "ah":    (499, 136, 241, 569),
    "bar":   (40, 1035, 620, 45),
    "coins": (1200, 18, 650, 55),
    "grid":  (508, 94, 1346, 986),
    "full":  None,
}

if REMOTE:
    REGIONS = REMOTE_REGIONS


def focus():
    if REMOTE:
        _remote_post("/focus", {})
        return
    subprocess.run(["/usr/bin/open", "-a", "/Applications/chiaki-ng.app"],
                   capture_output=True, timeout=10)


def fullscreen(on=True):
    """Only meaningful remotely; every crop assumes the stream fills the display."""
    if REMOTE:
        return _remote_post("/fullscreen", {"on": on}, timeout=90)
    raise RuntimeError("fullscreen is remote-only — set MUT_PAD_REMOTE")


CHIAKI_APP = os.environ.get("MUT_CHIAKI_APP", "/Applications/chiaki-ng.app")

# ⛔ MACHINE-SPECIFIC. Where the console's tile sits in chiaki's host list, in
# DISPLAY POINTS. This is the one coordinate that cannot be derived - it depends
# on the display size and on how many hosts chiaki has discovered.
#
# On a 1920x1080-point display with a single host it is ~(300, 175). On a
# different display it is NOT: the same machine measured (529, 231) at another
# resolution. Set MUT_CHIAKI_HOST_TILE="x,y" after checking a screenshot of the
# host list, or reconnect by hand and never call restart_stream.
HOST_TILE = tuple(int(v) for v in
                  os.environ.get("MUT_CHIAKI_HOST_TILE", "300,175").split(","))


def release(timeout=5):
    """Lift every key - AND MAKE SURE THE KEY-UPS REACH CHIAKI.

    ⛔⛔ /release posts key-up events to whatever window currently has FOCUS. It
    does not focus anything itself. So a release fired while chiaki is not
    frontmost lands in some other app and the button stays HELD on the console -
    and "chiaki is not frontmost" is precisely the situation that strands a
    key-down in the first place.

    The player watched the QB sprint for an entire game on a stuck R2,
    through several releases that all reported success. The daemon's
    "released: 24" is just the COUNT OF MAPPED KEYS - a constant - so it never
    indicated that anything had actually been lifted. Focus first, then lift.
    """
    try:
        _remote_post("/focus", {}, timeout=timeout)
        time.sleep(0.15)
    except Exception:
        pass
    return _remote_post("/release", {}, timeout=timeout)


def restart_stream(tile=HOST_TILE, tries=2):
    """Kill chiaki by PID and reconnect. The ONLY known cure for a dead renderer.

    chiaki stops presenting frames after roughly a minute while still decoding
    (32% CPU) and still forwarding input to the console. Activating the app
    does not revive it — verified with settles up to 5s. Restarting does.

    **This is safe and non-destructive.** Quitting chiaki releases the Remote
    Play session and touches nothing on the PS5: the game stays exactly where
    it was, cursor included. So the caller can re-run whatever step it was on.

    `pkill -x` is deliberately avoided — CLAUDE.md §3.2: quit-while-fullscreen
    silently survives, so TERM the PID then KILL it.
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
                # ⛔ A key held when the stream died never got its key-up - that
                # went to a window that no longer existed. The fresh stream then
                # inherits a jammed stick. Lift everything before handing back.
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
    """Send a whole button SEQUENCE in one daemon call.

    The daemon already walks `buttons` server-side:

        for b in buttons: press(b, hold=hold); time.sleep(gap)

    so a 50-press menu walk costs ONE http round trip instead of 50, and the
    per-press cost drops to hold+gap with no Python-side sleep in between.
    Measured on the TYPE-filter walk: ~29s -> ~9s.

    Use this for *menu navigation*, where dropped inputs are cheap to detect
    (the screen classifier catches it) and nothing destructive is one press
    away. Keep using press()/tap() for anything near a buy dialog, where the
    slower settle is the safety margin.

    Falls back to a plain loop when running fully local (no daemon).
    """
    names = list(names)
    if not names:
        return
    if REMOTE:
        return _remote_post("/press", {"buttons": names, "hold": hold, "gap": gap})
    for n in names:
        press(n, hold=hold)
        time.sleep(gap)


def combo(hold, taps, pre=0.30, tap_hold=0.06, gap=0.18, post=0.25, focus=True):
    """Hold one button DOWN while tapping others, in a single daemon call.

    Madden's play-call variant picker needs this: hold triangle, tap down x3,
    release triangle. The hold and the taps must not be separated by an HTTP
    round trip — jitter there lands inside the window the game is timing.

    Falls back to a best-effort local implementation when there is no daemon,
    but the daemon path is the real one.
    """
    taps = list(taps)
    if REMOTE:
        return _remote_post("/combo", {"hold": hold, "taps": taps, "pre": pre,
                                       "tap_hold": tap_hold, "gap": gap,
                                       "post": post, "focus": focus})
    hcode = KEYS[hold]
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateKeyboardEvent(None, hcode, True))
    try:
        time.sleep(pre)
        for t in taps:
            tcode = KEYS[t]
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                               Quartz.CGEventCreateKeyboardEvent(None, tcode, True))
            time.sleep(tap_hold)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                               Quartz.CGEventCreateKeyboardEvent(None, tcode, False))
            time.sleep(gap)
        time.sleep(post)
    finally:
        # Never leave the modifier stuck — that wedges the game entirely.
        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                           Quartz.CGEventCreateKeyboardEvent(None, hcode, False))


def sequence(events, focus=True, max_seconds=30.0):
    """Play a whole timed macro in ONE daemon call.

    `events` is a list of {"t": seconds from start, "key": name, "down": bool}.
    The daemon schedules every event on its own monotonic clock, so the timing
    between a key-down and its key-up - which is what the game reads - never has
    an HTTP round trip inside it. This is what a recorded macro replays through
    (`macrorec.py`). Anything still held at the end is released daemon-side.
    Returns the daemon's report: elapsed seconds and the worst lateness in ms.
    """
    events = [{"t": float(e["t"]), "key": e["key"], "down": bool(e["down"])}
              for e in events]
    if REMOTE:
        return _remote_post("/sequence", {"events": events, "focus": focus,
                                          "max_seconds": max_seconds})
    plan = sorted((e["t"], KEYS[e["key"]], e["down"]) for e in events)
    held = set()
    start = time.monotonic()
    try:
        for t, code, down in plan:
            delay = start + t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                               Quartz.CGEventCreateKeyboardEvent(None, code, down))
            (held.add if down else held.discard)(code)
    finally:
        for code in held:
            Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                               Quartz.CGEventCreateKeyboardEvent(None, code, False))
    return {"ok": True, "events": len(plan),
            "elapsed": round(time.monotonic() - start, 3)}


def press(name, hold=0.15, gap=None):
    """`gap` is the daemon's trailing sleep AFTER the button.

    It defaults to 0.35s server-side, which is dead time for a single press:
    every caller here sleeps its own settle immediately afterwards anyway. The
    buy sequence pays it five times, inside the race window - so tap() passes
    gap=0.0 and keeps its own wait, which it was doing regardless.
    """
    if REMOTE:
        # The daemon re-activates chiaki immediately before the press, which is
        # the part that actually matters (§14.1) — closer to the rule than the
        # local path's focus-sleep-press.
        body = {"buttons": [name], "hold": hold}
        if gap is not None:
            body["gap"] = gap
        _remote_post("/press", body)
        return
    code = KEYS[name]
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateKeyboardEvent(None, code, True))
    time.sleep(hold)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateKeyboardEvent(None, code, False))


def click(x, y, clicks=1):
    if REMOTE:
        _remote_post("/click", {"x": x, "y": y, "clicks": clicks})
        return
    btn = Quartz.kCGMouseButtonLeft
    mv = Quartz.CGEventCreateMouseEvent(None, Quartz.kCGEventMouseMoved, (x, y), btn)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, mv)
    time.sleep(0.12)
    for n in range(1, clicks + 1):
        for kind in (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp):
            ev = Quartz.CGEventCreateMouseEvent(None, kind, (x, y), btn)
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, n)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
            time.sleep(0.04)
        time.sleep(0.06)


def settled(region, out, display=1, tries=8, gap=1.5):
    """Capture until two consecutive frames match, then keep that frame.

    chiaki's frames lag well behind reality. An unchanged capture is an
    UNRESOLVED read, not proof a press failed - treating it as failure and
    re-pressing is what walks the cursor onto a listing detail page where
    cross = PLACE BID. Never re-press off a single capture.
    """
    import hashlib
    prev = None
    for _ in range(tries):
        shot(region, out, display)
        h = hashlib.md5(open(out, "rb").read()).hexdigest()
        if prev == h:
            return out, True          # stable - safe to act on
        prev = h
        time.sleep(gap)
    return out, False                 # never settled - do NOT act


def shot(region, out, display=1):
    if REMOTE:
        # FOCUS BEFORE CAPTURE — root-caused Aug 10, 2026.
        #
        # chiaki-ng stops PRESENTING frames when it is not frontmost. It keeps
        # decoding (measured 32% CPU, 26 threads, no errors logged), so nothing
        # fails — the picture simply stops updating, on the physical monitor as
        # well as in the capture. Every "frozen feed" chased across four
        # sessions was this: another window took focus during an idle gap.
        #
        # `press()` never hit it because the daemon calls focus_chiaki() before
        # every button (§14.1) — which is why long continuous scans never froze
        # and idle gaps always did. Captures had no such call, so they returned
        # a STALE screen that the tools then acted on. That asymmetry is the
        # whole bug, and stale reads driving a buy is how the CONFIRM BID
        # happened.
        #
        # Costs ~0.1s per capture. Set MUT_NO_CAPTURE_FOCUS=1 to skip it (e.g.
        # to avoid stealing focus while someone is using the mini) — but expect
        # stale frames if chiaki is not already frontmost.
        if not os.environ.get("MUT_NO_CAPTURE_FOCUS"):
            focus()
            time.sleep(0.25)
        # The daemon does the capture-twice-keep-the-second dance on its side.
        return _remote_shot(region, out, display)
    cmd = ["/usr/sbin/screencapture", "-x", f"-D{display}"]
    if region:
        x, y, w, h = region
        cmd.append(f"-R{x},{y},{w},{h}")
    cmd.append(out)
    # Twice: the first forces the hardware-rendered window buffer to refresh.
    for i in range(2):
        subprocess.run(cmd, capture_output=True, timeout=30)
        if i == 0:
            time.sleep(0.4)
    return out


if __name__ == "__main__":
    cmd = sys.argv[1]

    if cmd == "press":
        for b in sys.argv[2:]:
            if b not in KEYS:
                sys.exit(f"unknown button: {b}")
            # Re-activate before EVERY press, not once per invocation. chiaki
            # silently stops forwarding keys otherwise (workflow §14.1).
            focus()
            time.sleep(0.45)
            press(b)
            time.sleep(0.35)
        print("sent:", " ".join(sys.argv[2:]))

    elif cmd in ("shot", "settled"):
        name, out = sys.argv[2], sys.argv[3]
        region = REGIONS[name] if name in REGIONS else tuple(
            int(v) for v in name.split(","))
        if cmd == "settled":
            _, ok = settled(region, out)
            print("saved", out, "STABLE" if ok else "UNSETTLED-DO-NOT-ACT")
        else:
            shot(region, out)
            print("saved", out)

    elif cmd == "fullscreen":
        print(json.dumps(fullscreen(len(sys.argv) < 3 or sys.argv[2] != "off")))

    elif cmd == "restart":
        ok = restart_stream()
        print("restarted" if ok else "RESTART FAILED")
        sys.exit(0 if ok else 1)

    elif cmd == "where":
        if LOCAL_DAEMON:
            print("MODE: on-mini — localhost HTTP to the pad daemon (no ssh)")
        elif REMOTE:
            print("MODE: remote via ssh ->", REMOTE)
        else:
            print("MODE: local — this machine's own mouse/keyboard")

    elif cmd == "click":
        focus()
        time.sleep(0.6)
        click(float(sys.argv[2]), float(sys.argv[3]),
              int(sys.argv[4]) if len(sys.argv) > 4 else 1)
        print("clicked")
