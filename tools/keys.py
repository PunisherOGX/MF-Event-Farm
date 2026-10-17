#!/usr/bin/env python3
"""The Most Feared key ledger: which event keys we hold, and which event to
play next.

Every Trick-or-Treat event costs its own numbered key. Level 1 keys are bought
(Candy Store); a completed level pays a random key for the next level, and a
lost (or quit) run pays a key one level down (L1 pays L1). The farm always
plays the HIGHEST level (<= max_level) it holds a key for, so a fresh key is
used straight away; with no L2+ key it falls back to a bought L1 key.

    ./keys.py                 show the ledger and the L3/L4 loop record
    ./keys.py add 950 3       you bought 3x #950
    ./keys.py set 900 5       correct a count
    ./keys.py next            which event the farm would enter now
    ./keys.py log             the last 30 ledger changes

The ledger is ~/.mut_mf_keys.json; every change is appended to
~/.mut_mf_keys_log.jsonl with why. The L3/L4 loop record (one W/L per game
on those levels) is ~/.mut_mf_loop_record.json.
"""
import json
import os
import sys
import time

LEDGER = os.path.expanduser("~/.mut_mf_keys.json")
LOG = os.path.expanduser("~/.mut_mf_keys_log.jsonl")

# key -> (level, house name), in the order the events list shows them.
EVENTS = {
    "900": (1, "MURKWATER CROSSING"),
    "950": (1, "MURKWATER CROSSING"),
    "999": (1, "MURKWATER CROSSING"),
    "70": (2, "CATACOMB CIRCLE"),
    "80": (2, "CATACOMB CIRCLE"),
    "90": (2, "CATACOMB CIRCLE"),
    "100": (3, "PHANTASMAL PLACE"),
    "125": (3, "PHANTASMAL PLACE"),
    "150": (3, "PHANTASMAL PLACE"),
    "1988": (4, "ABYSSAL PARKWAY"),
}
ORDER = list(EVENTS)

# Only the FOCUSED card in the events list shows its description, and each one
# is unique - so this is how the focused card is identified. (Its number is
# not reliable: the timer overlaps it and OCR reads "#950" as "#95".)
DESCRIPTIONS = {
    "900": "GLOOMY MURKWATER",
    "950": "WHEN TERROR STRIKES",
    "999": "NOT ALWAYS ABOUT THE FINAL",
    "70": "SWEET CANDY FOOL",
    "80": "TWISTED WAY OF WELCOMING",
    "90": "MISS EVERY SHOT",
    "100": "BREAK YOUR ARM",
    "125": "LEGS TREMBLE",
    "150": "HUGE DEMANDS",
    "1988": "FINAL HOUSE",
}

# Which bought L1 key to spend first when several are held. #950 pays 10 for a
# 10+ yd rushing TD, which suits a run-only offence.
L1_PREFERENCE = ["950", "900", "999"]


def level(key):
    return EVENTS[str(key)][0]


def label(key):
    lv, house = EVENTS[str(key)]
    return f"LV{lv} {house.title()} #{key}"


def load():
    try:
        with open(LEDGER) as fh:
            d = json.load(fh)
        return {k: int(d.get(k, 0)) for k in ORDER}
    except (OSError, ValueError):
        return {k: 0 for k in ORDER}


def _save(d):
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as fh:
        json.dump({k: d[k] for k in ORDER}, fh, indent=1)
    os.replace(tmp, LEDGER)


def _log(key, delta, count, why):
    try:
        with open(LOG, "a") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                 "key": key, "delta": delta, "count": count,
                                 "why": why}) + "\n")
    except OSError:
        pass


def change(key, delta, why):
    key = str(key)
    if key not in EVENTS:
        raise ValueError(f"no such key #{key}")
    d = load()
    d[key] = max(0, d[key] + delta)
    _save(d)
    _log(key, delta, d[key], why)
    return d[key]


def set_count(key, n, why="set by hand"):
    key = str(key)
    if key not in EVENTS:
        raise ValueError(f"no such key #{key}")
    d = load()
    delta = n - d[key]
    d[key] = n
    _save(d)
    _log(key, delta, n, why)
    return n


def max_level():
    """Highest level the farm will ENTER (MUT_EVENT_MAX_LEVEL, default 4 for
    the L3 <-> L4 loop). Earned keys above it stay in the ledger."""
    try:
        return int(os.environ.get("MUT_EVENT_MAX_LEVEL", "4"))
    except ValueError:
        return 2


def pick_next(d=None):
    """The event to enter: the highest level (<= max_level) we hold a key for."""
    d = d or load()
    for lv in (4, 3, 2):
        if lv > max_level():
            continue
        for k in ORDER:
            if level(k) == lv and d[k] > 0:
                return k
    for k in L1_PREFERENCE:
        if d[k] > 0:
            return k
    return None


RECORD = os.path.expanduser("~/.mut_mf_loop_record.json")


def record_result(level, result, note=""):
    """Add one game to the L3/L4 loop record (W or L)."""
    try:
        rec = json.load(open(RECORD))
    except (OSError, ValueError):
        rec = {"L3": {"W": 0, "L": 0}, "L4": {"W": 0, "L": 0}, "log": []}
    k = f"L{level}"
    rec.setdefault(k, {"W": 0, "L": 0})[result] += 1
    rec.setdefault("log", []).append({"ts": time.strftime("%Y-%m-%dT%H:%M"),
                                      "level": level, "result": result,
                                      "note": note})
    tmp = RECORD + ".tmp"
    json.dump(rec, open(tmp, "w"), indent=1)
    os.replace(tmp, RECORD)


def show():
    d = load()
    for lv in (1, 2, 3, 4):
        row = [f"#{k}: {d[k]}" for k in ORDER if level(k) == lv]
        print(f"  LV{lv}  " + "   ".join(row))
    nxt = pick_next(d)
    try:
        rec = json.load(open(RECORD))
        print("  loop record: " + "   ".join(
            f"{k} {rec[k]['W']}-{rec[k]['L']}" for k in ("L3", "L4") if k in rec)
              + f"   (since {rec.get('since', '?')[:16]})")
    except (OSError, ValueError, KeyError):
        pass
    print(f"  next -> {label(nxt) if nxt else 'NO KEYS - buy L1 keys'}"
          f"   (farm enters up to LV{max_level()})")


if __name__ == "__main__":
    a = sys.argv[1:]
    if not a:
        show()
    elif a[0] == "add" and len(a) == 3:
        print(f"  #{a[1]} -> {change(a[1], int(a[2]), 'bought')}")
    elif a[0] == "set" and len(a) == 3:
        print(f"  #{a[1]} -> {set_count(a[1], int(a[2]))}")
    elif a[0] == "next":
        k = pick_next()
        print(label(k) if k else "NO KEYS")
    elif a[0] == "log":
        try:
            with open(LOG) as fh:
                for line in fh.readlines()[-30:]:
                    print(" ", line.rstrip())
        except OSError:
            print("  (no log yet)")
    else:
        print(__doc__)
