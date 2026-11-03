"""MIDI tools on clip notes (pure Python on top of notes / set_notes) and text patterns."""
import json
import random

from . import cli as C
from .client import LiveError, call

GRID = {"1/4": 1.0, "1/8": 0.5, "1/8T": 1 / 3.0, "1/16": 0.25, "1/16T": 1 / 6.0, "1/32": 0.125}


def _grid(v):
    if v in GRID:
        return GRID[v]
    try:
        return float(v)
    except ValueError:
        raise LiveError("grid: one of %s or beats" % ", ".join(GRID))


def _kv(items):
    out = {}
    pos = []
    for it in items:
        if "=" in it:
            k, v = it.split("=", 1)
            out[k] = v
        elif not it.startswith("--"):
            pos.append(it)
    return out, pos


def read_notes(ref, cr):
    return call("notes", track=ref, **cr)


def write_notes(ref, cr, notes, add=False):
    clean = [{"pitch": int(max(0, min(127, n["pitch"]))), "start": max(0.0, float(n["start"])),
              "duration": max(1e-3, float(n["duration"])), "velocity": float(max(1, min(127, n.get("velocity", 100)))),
              "mute": bool(n.get("mute", False))} for n in notes]
    return call("set_notes", track=ref, notes=clean, add=add, **cr)


def clip_length(ref, cr):
    return call("clip_set", track=ref, **cr)["length"]


def scale_of_song():
    s = call("song")
    root = int(s.get("root_note", 0))
    iv = s.get("scale_intervals") or [0, 2, 4, 5, 7, 9, 11]
    return root, [int(x) for x in iv], s.get("scale_name")


def transpose_in_scale(pitch, steps, root, intervals):
    """Move a pitch by `steps` scale degrees; notes outside the scale snap to the nearest degree below first."""
    pcs = sorted(set((root + i) % 12 for i in intervals))
    octave, pc = divmod(pitch, 12)
    below = [p for p in pcs if p <= pc]
    deg = pcs.index(below[-1]) if below else -1
    offset = pc - (below[-1] if below else pcs[-1] - 12)
    if not below:
        octave -= 1
        deg = len(pcs) - 1
    deg += steps
    o2, d2 = divmod(deg, len(pcs))
    return (octave + o2) * 12 + pcs[d2] + offset


def euclid(pulses, steps, rotate=0):
    """Bjorklund-style even distribution of `pulses` onsets over `steps`."""
    if pulses <= 0:
        return [0] * steps
    pat = [1 if (k * pulses) % steps < pulses else 0 for k in range(steps)]
    rotate %= steps
    return pat[-rotate:] + pat[:-rotate] if rotate else pat


@C.command("notes-edit", "notes-edit <track> <slot|@arr> <op> [key=value...]", "MIDI tools: humanize (timing=0.02 "
           "velocity=8 seed=1), swing (amount=55 grid=1/16), ratchet (n=2), retrograde, arp (mode=up|down|updown|random "
           "rate=1/16 gate=0.8), scale (steps=2: transpose by degrees of the song scale), legato, velocity-curve "
           "(min=40 max=120). One undo step", "clips")
def c_notes_edit(a):
    C.need(a, 3, "notes-edit <track> <clip> <op> [key=value...]")
    t = C.track(a[0])
    ref, cr, op = t["ref"], C.clip_ref(a[1]), a[2]
    kw, _ = _kv(a[3:])
    notes = read_notes(ref, cr)
    if not notes:
        raise LiveError("the clip has no notes")
    out = []
    if op == "humanize":
        rnd = random.Random(int(kw.get("seed", 1)))
        tm, vel = float(kw.get("timing", 0.02)), float(kw.get("velocity", 8))
        for n in notes:
            out.append(dict(n, start=max(0.0, n["start"] + rnd.uniform(-tm, tm)), velocity=n["velocity"] + rnd.uniform(-vel, vel)))
    elif op == "swing":
        g = _grid(kw.get("grid", "1/16"))
        amt = float(kw.get("amount", 55)) / 100.0          # 50 = straight, 66 = triplet feel
        for n in notes:
            k = round(n["start"] / g)
            if abs(n["start"] - k * g) < g * 0.25 and k % 2 == 1:
                n = dict(n, start=(k - 1) * g + 2 * g * amt)
            out.append(n)
    elif op == "ratchet":
        r = int(kw.get("n", 2))
        for n in notes:
            d = n["duration"] / r
            out += [dict(n, start=n["start"] + i * d, duration=d * 0.9) for i in range(r)]
    elif op == "retrograde":
        length = clip_length(ref, cr)
        out = [dict(n, start=max(0.0, length - n["start"] - n["duration"])) for n in notes]
    elif op == "arp":
        mode, rate, gate = kw.get("mode", "up"), _grid(kw.get("rate", "1/16")), float(kw.get("gate", 0.8))
        rnd = random.Random(int(kw.get("seed", 1)))
        chords = {}
        for n in notes:
            chords.setdefault(round(n["start"], 4), []).append(n)
        for st, ch in sorted(chords.items()):
            ps = sorted(set(n["pitch"] for n in ch))
            seq = ps if mode == "up" else ps[::-1] if mode == "down" else ps + ps[-2:0:-1] if mode == "updown" else None
            dur = max(n["duration"] for n in ch)
            vel = max(n["velocity"] for n in ch)
            k, tm = 0, st
            while tm < st + dur - 1e-6:
                p = seq[k % len(seq)] if seq else rnd.choice(ps)
                out.append({"pitch": p, "start": tm, "duration": rate * gate, "velocity": vel})
                k += 1
                tm += rate
    elif op == "scale":
        root, iv, name = scale_of_song()
        steps = int(kw.get("steps", 1))
        out = [dict(n, pitch=transpose_in_scale(n["pitch"], steps, root, iv)) for n in notes]
    elif op == "legato":
        sn = sorted(notes, key=lambda n: (n["start"], n["pitch"]))
        starts = sorted(set(n["start"] for n in sn))
        length = clip_length(ref, cr)
        for n in sn:
            nxt = next((s for s in starts if s > n["start"] + 1e-6), length)
            out.append(dict(n, duration=nxt - n["start"]))
    elif op == "velocity-curve":
        lo, hi = float(kw.get("min", 40)), float(kw.get("max", 120))
        vs = [n["velocity"] for n in notes]
        a_, b_ = min(vs), max(vs)
        out = [dict(n, velocity=lo + (n["velocity"] - a_) * (hi - lo) / ((b_ - a_) or 1)) for n in notes]
    else:
        raise LiveError("op: humanize, swing, ratchet, retrograde, arp, scale, legato, velocity-curve")
    r = write_notes(ref, cr, out)
    C.out(r, "%s: %d notes -> %d notes" % (op, len(notes), r["notes"]))


@C.command("pattern", "pattern <track> <slot|@arr> <x..X....> [pitch=36] [step=1/16] [velocity=100] [accent=127] "
           "[length=0.9] [--add]", "write notes from a text pattern: x = hit, X = accent, . or - = rest, | ignored; "
           "the pattern repeats over the clip", "clips")
def c_pattern(a):
    C.need(a, 3, "pattern <track> <clip> <pattern> [key=value...]")
    t = C.track(a[0])
    ref, cr = t["ref"], C.clip_ref(a[1])
    pat = a[2].replace("|", "").replace(" ", "")
    if not pat or set(pat) - set("xX.-"):
        raise LiveError("pattern: use x, X, . and - (e.g. x...x...x.x.x...)")
    kw, _ = _kv(a[3:])
    pitch, step = int(kw.get("pitch", 36)), _grid(kw.get("step", "1/16"))
    vel, acc, gate = float(kw.get("velocity", 100)), float(kw.get("accent", 127)), float(kw.get("length", 0.9))
    length = clip_length(ref, cr)
    notes, k = [], 0
    while k * step < length - 1e-6:
        ch = pat[k % len(pat)]
        if ch in "xX":
            notes.append({"pitch": pitch, "start": k * step, "duration": step * gate, "velocity": acc if ch == "X" else vel})
        k += 1
    r = write_notes(ref, cr, notes, add="--add" in a)
    C.out(r, "pattern written: %d hits over %g beats (clip now has %d notes)" % (len(notes), length, r["notes"]))


@C.command("euclid", "euclid <track> <slot|@arr> <pulses> <steps> [pitch=36] [step=1/16] [velocity=100] [rotate=0] [--add]",
           "Euclidean rhythm (e.g. 3 8 = tresillo, 5 8, 7 16) repeated over the clip", "clips")
def c_euclid(a):
    C.need(a, 4, "euclid <track> <clip> <pulses> <steps> [key=value...]")
    kw, _ = _kv(a[4:])
    pat = "".join("x" if v else "." for v in euclid(int(a[2]), int(a[3]), int(kw.get("rotate", 0))))
    rest = [x for x in a[4:] if not x.startswith("rotate=")]
    c_pattern([a[0], a[1], pat] + rest)
