# MF Event Farm

Plays the Madden NFL 27 Ultimate Team **Most Feared: Trick-or-Treat CPU
events** on a PS5 by itself, looping **Level 3 <-> Level 4**. It watches the
console over a [chiaki-ng](https://github.com/streetpea/chiaki-ng) Remote Play
stream on a Mac, reads the screen with Apple's Vision OCR, and presses buttons
through a small local daemon that drives chiaki's keyboard-as-controller input.

    read the screen -> work out the situation -> act -> repeat

Nothing is time-based: every pass re-reads the screen, so long plays,
penalties, turnovers and cutscenes are all handled the same way. When a read
is not confident, the loop waits rather than pressing blind.

---

## The event

PLAY tab -> tile **MF: TRICK OR TREAT CPU EVENTS** -> a list of 10 events in 4
levels. Every event costs its own numbered **key** (Entry Options: 1 key).

| Level | Events (key numbers) | Mode |
|---|---|---|
| LV1 Murkwater Crossing | #900, #950, #999 | Pro, Arcade |
| LV2 Catacomb Circle | #70, #80, #90 | All Pro, Arcade |
| LV3 Phantasmal Place | #100, #125, #150 | Pro, Competitive |
| LV4 FINAL Abyssal Parkway | #1988 | All Pro, Competitive |

- vs CPU, 11on11, Active MUT Team, **No Kicks/Punts**, points can be **stolen**.
- Games start in **Q3** (3:00 quarters), L1-L3 down 0-5, L4 down 0-10.
- **3 wins completes a level; one loss ends a run.**
- Finishing a level pays a key for the next level. **A loss (or a quit) pays
  a key one level down** (L4 -> L3, L3 -> L2, L2 -> L1, L1 -> L1). Both arrive
  the same way: *Finish Event* on the card -> a "Most Feared Level N CPU Event
  Key" pack in the rewards -> open it -> the key.
- An L4 run that ends after one win still pays candy + an L3 key.

## The loop

    L3 run (3 wins) -> Finish Event -> open the key pack -> read the L4 key
      -> enter L4 #1988 -> play game 1
           won  -> start game 2 and QUIT it at once
                   (pause -> UP to QUIT GAME -> X -> "YES - EXIT TO MENU")
                   -> progress screen 1/1 LOSSES -> Finish Event
           lost -> Finish Event
      -> open the key pack -> read the L3 key -> L3 run -> ...

The farm always enters the **highest-level event it holds a key for**
(`keys.py`, capped by `MUT_EVENT_MAX_LEVEL`, default 4). So a loss on L3 pays
an L2 key, the farm plays L2, the L2 completion pays an L3 key, and the loop
carries on by itself. With no L2+ key it falls back to a bought L1 key.

## What it does in a game

| | |
|---|---|
| Offence, not winning | **INSIDE ZONE** on **X** in the offensive FAVORITES. Select, wait the 6.0s snap floor, snap, then left stick UP + R2 (sprint) from +1.0s for up to 3.0s. The daemon lets go the moment the playcall screen comes back |
| Offence, winning | **FB DIVE WEAK** on **triangle**, same hold. "Winning" = the opponent's score has been read and ours is strictly higher; a tie or a deficit goes back to Inside Zone. Once up **10+**, a short hold (0.2s / 1.5s) |
| Defence | **MID BLITZ** on **square** in the defensive FAVORITES, plain press |
| Clock | Level 1 chews all game. Level 2+ stays on **NORMAL** tempo with no play-clock wait until winning, then sets **CHEW CLOCK** (R3 -> Tempo) and snaps at :14 while the game clock runs |
| 4th down | never punts, never kicks: goes back to FAVORITES and runs the play |
| Between games | every menu is identified by its OCR text: postgame -> progress -> Finish Event / rewards -> key pack -> MF hub tile -> MF events list -> key picker / spend confirm -> next game |

Before pressing a play button it checks the visible favourites row shows that
play (stray stick input scrolls the rows). Scores are validated before they
are believed (bounded jumps, a lower score must repeat, OCR look-alike digits
mapped back), and the event's own progress screen (`x/3 WINS`, `x/1 LOSSES`)
is what decides wins and losses.

### The favourites it expects

Set these up in Madden before running:

| Side | FAVORITES slot | Play |
|---|---|---|
| Offence | **X** (cross) | INSIDE ZONE |
| Offence | **triangle** | FB DIVE WEAK |
| Defence | **square** | MID BLITZ |

Don't put anything you would not want run on circle: circle is the loop's
"back out" key. X is also the snap button.

---

## Requirements

- **macOS**, with the stream on a **1920x1080-point display**, chiaki-ng
  fullscreen, no letterbox. Every crop region assumes this.
- **[chiaki-ng](https://github.com/streetpea/chiaki-ng)** connected to the PS5
  (wired network strongly recommended), with **Keyboard as controller** on its
  default map, **except Left Stick Up rebound to F13** (the default, "Ins",
  does not exist on a Mac keyboard):

      defaults write com.chiaki.Chiaki "keymap.left_stick_up" F13

- **Python 3.9+** with pyobjc (Quartz, Vision).
- **The pad daemon** (`mini-pad/padd.py`) running in the logged-in GUI session.
  It posts keystrokes and captures the screen, so it needs **Screen Recording**
  and **Accessibility** for the Python that runs it. While it runs, this Mac's
  keyboard focus belongs to chiaki: use a machine nobody is sitting at.

## Setup

```bash
git clone <this repo> ~/MF-Event-Farm && cd ~/MF-Event-Farm

# 1. Python venv (farm.sh / status.sh look for ./venv first)
python3 -m venv venv
venv/bin/pip install pyobjc-framework-Quartz pyobjc-framework-Vision

# 2. The daemon's auth token (it refuses to start without one)
mkdir -p ~/mut-pad
head -c 32 /dev/urandom | xxd -p > ~/mut-pad/token
chmod 600 ~/mut-pad/token

# 3. Start the daemon in the GUI session (from Terminal, or as a LaunchAgent)
venv/bin/python mini-pad/padd.py      # listens on 127.0.0.1:8773
```

Grant Screen Recording + Accessibility to whatever runs `padd.py` (Terminal,
or the venv's Python if you load it as a LaunchAgent). A minimal LaunchAgent is
`~/Library/LaunchAgents/local.mutpad.plist` with `ProgramArguments` =
`[<repo>/venv/bin/python, <repo>/mini-pad/padd.py]`, `RunAtLoad` and
`KeepAlive` true; load it with `launchctl load -w <plist>`.

### Machine-specific settings

| Variable | Default | What |
|---|---|---|
| `MUT_PS5_HOST` | `192.168.1.100` (placeholder) | your console's IP (PS5 Settings -> Network -> Connection Status). **Set this.** It is pinged before starting and whenever a screen looks static |
| `MUT_EVENT_OUR_CODES` | (empty) | the label(s) the scorebar shows for **your** side, space-separated: usually the first three letters of your gamertag, and/or your team code, e.g. `"ABC LV"`. Without it the score reader may not find your score |
| `MUT_CHIAKI_HOST_TILE` | `300,175` | where your console's tile sits in chiaki's host list, in display points. Used only to reconnect a dropped stream |
| `MUT_CHIAKI_APP` | `/Applications/chiaki-ng.app` | chiaki-ng's location |
| `MUT_PAD_TOKEN_FILE` | `~/mut-pad/token` | the daemon token (client and daemon both read it) |
| `MUT_EVENT_MAX_LEVEL` | `4` | the highest level the farm will enter |

Every game setting in `tools/config.py` can be overridden by the environment
variable named next to it (plays, buttons, timings, thresholds).

---

## Run it

```bash
cd tools
export MUT_PS5_HOST=<your PS5 IP>
export MUT_EVENT_OUR_CODES="<your scorebar label>"

./calibrate.py shot          # a full-screen PNG of what the farm sees
./calibrate.py verify        # once, during a live game: signs off the crops and
                             # writes ~/.mut_event_calibration.json; grind.py
                             # refuses to start without it

./keys.py                    # the key ledger + the L3/L4 loop record
./keys.py add 950 3          # you bought 3x #950 (L1 keys)
./keys.py set 125 1          # correct a count by hand
./keys.py next               # which event the farm would enter now

./farm.sh                    # start. Logs to /tmp/mut-event/grind-YYYYMMDD.log
./health.sh                  # the watcher - run it in the background right
                             # after farm.sh; it EXITS (with the reason) when
                             # something needs a human
./status.sh                  # last 10 games, record, keys, runs, alerts
./farm.sh stop               # stop cleanly at the next play boundary
```

Start it from the MF events list, a playcall screen, or any between-game
screen. `health.sh` exits when the loop dies, a HALT or FROZEN file appears, a
new sticky alert is written, the log goes stale (7 min) or no game finishes in
45 min, so launch it with something that tells you when it exits (a plain
`nohup ... &` does not).

**Pause before the next run:** `touch /tmp/mut-event/pause_before_new_run`
and the farm halts on the events list *before* joining a new run (nothing
pressed). Remove the file and the HALT to carry on.

**Taking over the controller:** kill the loop (`pkill -f "grind.py loop"`) and
play. Don't press anything from the Mac while you have the pad. If you finish
or lose a run by hand, fix the ledger with `keys.py set` before restarting.

**After a HALT:** read `/tmp/mut-event/HALT`, fix what it says, then
`rm /tmp/mut-event/HALT && ./farm.sh`. `farm.sh` will not start over it.

## Files

| File | What |
|---|---|
| `tools/config.py` | every event setting: plays, timings, screen regions, files |
| `tools/grind.py` | the loop and the between-game menus (`./grind.py loop`) |
| `tools/actions.py` | everything that presses a button |
| `tools/screen.py` · `tools/hud.py` · `tools/ocr.py` | everything that reads the screen. `hud.py` finds the clock and score by content, so it works on every broadcast layout |
| `tools/keys.py` | the key ledger and next-event picker |
| `tools/calibrate.py` | `shot` and `verify` (read-only) |
| `tools/pad.py` · `mini-pad/padd.py` | the daemon client and the daemon (keystrokes + in-process screen capture, 127.0.0.1 only, token auth) |
| `tools/farm.sh` · `health.sh` · `status.sh` | start/stop · watcher · report |

### Files it writes (all outside the repo)

| Path | What |
|---|---|
| `~/.mut_mf_keys.json` | the key ledger (counts per key number) |
| `~/.mut_mf_keys_log.jsonl` | every ledger change, with why |
| `~/.mut_mf_loop_record.json` | W/L per game on L3 and L4 |
| `~/.mut_most_feared_history.jsonl` | one row per completed game: **trust this, not the log** |
| `~/.mut_most_feared_runs.jsonl` | run events: started, progress, COMPLETE, LOST, quit, key spent/earned/refunded |
| `~/.mut_event_state.json` | the run in progress (survives restarts) |
| `~/.mut_event_calibration.json` | the `calibrate.py verify` stamp |
| `/tmp/mut-event/` | logs, HALT / ALERT / ALERT.STICKY / FROZEN files, `stuck/` screenshots for anything it could not handle |

## Recent changes (Oct 17-18)

- **Stat tickers are never read as scores.** A HUD band containing RUSH/YDS/
  REC/TKL/... words is skipped, so "3 RUSHES 41 YDS" can't become 3-41.
- **Offensive playcall with no tab strip yet** waits for 3 reads before backing
  out with circle (it used to back out of a screen that was still drawing).
- **Games are labelled with the event they were played in**, so a game that
  finishes after the bot walks into the next event is still booked to the right
  one. The score-reader "GAME LOST" alert is no longer sticky: the progress
  screen is the truth.
- **"YOUR | REWARDS" split by OCR** is re-joined, so the key pack on the
  rewards screen is opened instead of skipped.
- **Key picker**: only rejects a read that names a *different* key (a misread
  "150" as "60" no longer aborts entry).
- **Candy tracking**: counted from results, not the on-screen total (which
  moves with spending): 2,000 per L3 completion, 2,500 per L4 game-1 win.
  `./keys.py` prints it.
- **A Madden crash mid-game ends the run** (it counts as a loss). The farm can't
  relaunch Madden: kill it, dismiss the PS5 error, relaunch, go to the MF
  events tile, restart.

Record with this setup (Inside Zone until winning, FB Dive Weak once ahead,
Mid Blitz): L3 103-8, L4 game 1 30-3 since Oct 16.

---

## Good to know

- **Look at the screen before changing code.** `calibrate.py shot` shows
  exactly what the farm sees, and `/tmp/mut-event/stuck/` holds a PNG + OCR
  text for every screen it gave up on.
- **The key ledger is the farm's belief, the game is the truth.** The key
  picker's "N ITEMS FOUND" re-syncs the count on every entry; if you spend or
  earn keys by hand, `keys.py set` them.
- **A dropped or frozen stream** is restarted automatically (chiaki is killed
  and reconnected; the game on the PS5 is untouched). An EA sign-out is not.
- **No anti-detection layer**, by choice. Automating play may be against EA's
  terms for your account; that is your call.
