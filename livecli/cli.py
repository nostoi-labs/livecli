"""live: a command line for Ableton Live, built for AI agents (short text output, --json everywhere)."""
import glob
import importlib.util
import json
import os
import re
import statistics
import sys
import time

from . import probes as P
from .client import CONFIG_DIR, EXIT_CODES, LiveError, call, config
from .resolve import all_devices, find_device, find_param, find_track, parse_number

COMMANDS = {}      # name -> (function, usage, help, group)
ALIASES = {}
JSON = False


def command(name, usage="", help="", group="other", aliases=()):
    def deco(f):
        if name in ALIASES:
            raise RuntimeError("command %s shadowed by an alias" % name)
        COMMANDS[name] = (f, usage, help, group)
        for a in aliases:
            if a in COMMANDS:
                raise RuntimeError("alias %s shadows a command" % a)
            ALIASES[a] = name
        return f
    return deco


def out(obj, text=None):
    """Print obj as JSON with --json, otherwise the text (or a compact JSON)."""
    if JSON or text is None:
        print(json.dumps(obj, indent=None if JSON else 1, ensure_ascii=False))
    else:
        print(text)


def need(a, n, usage):
    if len(a) < n:
        raise LiveError("usage: live " + usage)


def onoff(v):
    return "on " if v else ("off" if v is not None else " - ")


def tracks():
    return call("tracks")


def track(q):
    return find_track(tracks(), q)


def bar_to_beats(bar, song=None):
    song = song or call("song")
    n, d = [int(x) for x in song["signature"].split("/")]
    return (float(bar) - 1) * n * 4.0 / d


def print_devices(devs, ind="    "):
    for d in devs:
        extra = ""
        if d.get("latency_samples"):
            extra += "  latency %d smp (%.2f ms)" % (d["latency_samples"], d.get("latency_ms", 0))
        if d.get("ab") == "B":
            extra += "  [B]"
        print("%s[%s] %s %s%s" % (ind, d["path"], onoff(d["on"]), d["name"], extra))
        for ch in d.get("chains", []):
            print("%s    chain: %s" % (ind, ch["name"]))
            print_devices(ch["devices"], ind + "        ")


# ================================================================== info
@command("ping", "ping", "check the connection to Live", "info")
def c_ping(a):
    r = call("ping")
    out(r, "pong (LiveCLI remote script v%s)" % r["version"])


@command("song", "song", "tempo, signature, transport, loop, counts", "info", aliases=("stato", "status"))
def c_song(a):
    s = call("song")
    out(s, "\n".join("%-15s %s" % (k, v) for k, v in s.items()))


@command("cpu", "cpu [seconds]", "Live's CPU meter (average and peak); with seconds, sampled every 0.5 s", "info")
def c_cpu(a):
    sec = float(a[0]) if a else 0
    if not sec:
        r = call("cpu")
        return out(r, "Live CPU: average %.0f%%, peak %.0f%%" % (r["average_process_usage"], r["peak_process_usage"]))
    av, pk = [], []
    t0 = time.time()
    while time.time() - t0 < sec:
        r = call("cpu")
        av.append(r["average_process_usage"])
        pk.append(r["peak_process_usage"])
        time.sleep(0.5)
    r = {"seconds": sec, "samples": len(av), "average": statistics.mean(av), "average_max": max(av),
         "peak_max": max(pk), "peaks_over_100": sum(1 for x in pk if x > 100)}
    out(r, "Live CPU over %gs (%d samples): average %.0f%% (max %.0f%%), peak max %.0f%%, peak >100%% in %d samples"
        % (sec, len(av), r["average"], r["average_max"], r["peak_max"], r["peaks_over_100"]))


@command("tracks", "tracks [filter]", "all tracks, returns and master with output and devices", "info", aliases=("tracce",))
def c_tracks(a):
    f = a[0].lower() if a else ""
    ts = [t for t in tracks() if not f or f in t["name"].lower()]
    if JSON:
        return out(ts)
    for t in ts:
        extra = []
        if t.get("output"):
            extra.append("-> " + t["output"])
        for k, lab in (("mute", "MUTE"), ("solo", "SOLO"), ("arm", "ARM"), ("frozen", "FROZEN")):
            if t.get(k):
                extra.append(lab)
        devs = [d["name"] + ("" if d["on"] is not False else " (off)") for d in t["devices"]]
        print("%-4s %-28s %-24s %s" % (t["ref"], t["name"][:28], " ".join(extra)[:24], ", ".join(devs)))


@command("devices", "devices <track>", "device chain of a track, racks included (paths like 2.0.1)", "info", aliases=("device",))
def c_devices(a):
    need(a, 1, "devices <track>")
    t = track(a[0])
    if JSON:
        return out(t["devices"])
    print("%s %s" % (t["ref"], t["name"]))
    print_devices(t["devices"])


@command("params", "params <track> <device> [filter]", "parameters of a device with their display text", "info", aliases=("parametri",))
def c_params(a):
    need(a, 2, "params <track> <device> [filter]")
    t = track(a[0])
    d = find_device(t, a[1])
    f = a[2].lower() if len(a) > 2 else ""
    r = call("params", track=t["ref"], device=d["path"])
    ps = [p for p in r["params"] if not f or f in p["name"].lower()]
    if JSON:
        return out(dict(r, params=ps))
    print("%s / %s (%s)" % (t["name"], r["device"], r["class"]))
    for p in ps:
        print("  %3d %-32s %s" % (p["i"], p["name"][:32], p["text"]))


@command("mixer", "mixer <track>", "volume, pan and sends of a track", "info")
def c_mixer(a):
    need(a, 1, "mixer <track>")
    r = call("mixer", track=track(a[0])["ref"])
    if JSON:
        return out(r)
    print("%s  volume %s  pan %s" % (r["track"], r["volume"], r["pan"]))
    for m in r["sends"]:
        print("  send %s %-26s %s" % (chr(65 + m["i"]), m["return"][:26], m["value"]))


@command("meters", "meters [seconds] [filter]", "max output peak of every track (Live's meter scale 0-1)", "info", aliases=("livelli", "levels"))
def c_meters(a):
    sec = float(a[0]) if a else 3
    f = a[1].lower() if len(a) > 1 else ""
    names = {t["ref"]: t["name"] for t in tracks()}
    mx = {}
    t0 = time.time()
    while time.time() - t0 < sec:
        for ref, (l, r) in call("meters").items():
            mx[ref] = max(mx.get(ref, 0), l, r)
        time.sleep(0.05)
    rows = [{"ref": k, "name": names.get(k, ""), "peak": v} for k, v in mx.items() if not f or f in names.get(k, "").lower()]
    if JSON:
        return out(rows)
    print("Live meter peaks over %gs (Live's 0-1 meter scale, not dBFS; use probes for real levels)" % sec)
    for r in rows:
        print("  %-4s %-28s %.3f %s" % (r["ref"], r["name"][:28], r["peak"], "#" * int(r["peak"] * 40)))


@command("routing", "routing <track> [type|-] [channel]", "show or set the output routing", "info")
def c_routing(a):
    need(a, 1, "routing <track> [type|-] [channel]")
    t = track(a[0])
    if len(a) > 1:
        return out(call("set_routing", track=t["ref"], side="output", type=a[1] if a[1] != "-" else None,
                        channel=a[2] if len(a) > 2 else None))
    r = call("routing", track=t["ref"])
    if JSON:
        return out(r)
    print("%s -> %s / %s" % (t["name"], r["output"], r["output_channel"]))
    print("  output types: " + " | ".join(r["output_types"]))
    print("  output channels: " + " | ".join(r["output_channels"]))
    if r.get("input_types"):
        print("  input: %s / %s" % (r["input"], r["input_channel"]))
        print("  input types: " + " | ".join(r["input_types"]))
        print("  input channels: " + " | ".join(r["input_channels"]))


@command("input", "input <track> [type|-] [channel]", "show or set the input routing", "info", aliases=("ingresso",))
def c_input(a):
    need(a, 1, "input <track> [type|-] [channel]")
    t = track(a[0])
    if len(a) > 1:
        return out(call("set_routing", track=t["ref"], side="input", type=a[1] if a[1] != "-" else None,
                        channel=a[2] if len(a) > 2 else None))
    r = call("routing", track=t["ref"])
    out({k: v for k, v in r.items() if k.startswith("input")})


@command("locators", "locators", "arrangement locators with their bar", "info", aliases=("locatori",))
def c_locators(a):
    ls = call("locators")
    out(ls, "\n".join("  bar %6g  %s" % (round(l["bar"], 3), l["name"]) for l in ls) or "no locators")


@command("scenes", "scenes", "session scenes", "info")
def c_scenes(a):
    sc = call("scenes")
    out(sc, "\n".join("  %3d %s%s" % (s["i"], s["name"], "  (tempo %g)" % s["tempo"] if s.get("tempo") else "") for s in sc))


@command("clips", "clips [track]", "session clips (all tracks or one)", "info")
def c_clips(a):
    r = call("clips", track=track(a[0])["ref"] if a else None)
    if JSON:
        return out(r)
    for c in r:
        print("  %-4s %-20s slot %-3d %-24s %6g beats %s%s" % (c["track"], c["track_name"][:20], c["slot"], c["name"][:24],
              c["length"], "midi" if c["midi"] else "audio", " PLAYING" if c["playing"] else ""))


@command("arrangement", "arrangement [track]", "arrangement clips with start and end bar", "info", aliases=("arrangiamento",))
def c_arrangement(a):
    r = call("arrangement", track=track(a[0])["ref"] if a else None)
    if JSON:
        return out(r)
    for c in r:
        print("  %-4s %-20s bar %7.2f - %7.2f  %s%s" % (c["track"], c["track_name"][:20], c["start_bar"], c["end_bar"],
              c["name"], " (muted)" if c["muted"] else ""))


def clip_ref(q):
    """'3' = session slot 3; '@2' = third clip of the arrangement (in time order, see 'live arrangement')."""
    q = str(q)
    return {"arr": int(q[1:])} if q.startswith("@") else {"slot": int(q)}


@command("notes", "notes <track> <slot|@arr>", "MIDI notes of a clip (JSON); @N = arrangement clip N", "info")
def c_notes(a):
    need(a, 2, "notes <track> <slot|@arr>")
    print(json.dumps(call("notes", track=track(a[0])["ref"], **clip_ref(a[1]))))


@command("search", "search <text> [roots]", "search the browser; roots: user_library,audio_effects,instruments,plugins,"
         "max_for_live,drums,sounds,samples,clips,packs,current_project", "browser", aliases=("cerca",))
def c_search(a):
    need(a, 1, "search <text> [roots]")
    r = call("search", q=a[0], roots=a[1].split(",") if len(a) > 1 else None, timeout=30)
    out(r, "\n".join(r) or "nothing found")


@command("browse", "browse [path]", "list a browser folder, e.g. 'plugins/VST3'", "browser")
def c_browse(a):
    r = call("browse", path=a[0] if a else None, timeout=20)
    if JSON or not a:
        return out(r, "\n".join(r) if not a else None)
    for x in r:
        print("  %s%s" % (x["name"], "/" if x["folder"] else ""))


# ================================================================== transport
@command("play", "play", "start playback", "transport")
def c_play(a):
    call("play")
    out(True, "playing")


@command("stop", "stop", "stop playback", "transport")
def c_stop(a):
    call("stop")
    out(True, "stopped")


@command("continue", "continue", "continue from the current position", "transport")
def c_continue(a):
    call("continue")
    out(True, "playing")


@command("back-to-arrangement", "back-to-arrangement", "Live's Back to Arrangement button", "transport")
def c_back(a):
    call("set_song", back_to_arranger=True)
    out(True, "back to arrangement")


@command("stop-clips", "stop-clips", "stop all session clips", "transport")
def c_stop_clips(a):
    out(call("stop_clips"), "clips stopped")


@command("record", "record on|off", "arrangement record button", "transport")
def c_record(a):
    r = call("record", on=(a[0] == "on")) if a else call("record")
    out(r, "record %s" % ("on" if r else "off"))


@command("undo", "undo", "undo the last change in Live", "transport")
def c_undo(a):
    out(call("undo"), "undone")


@command("redo", "redo", "redo", "transport")
def c_redo(a):
    out(call("redo"), "redone")


@command("jump", "jump <bar>", "move the playhead to a bar (1 = start)", "transport", aliases=("vai",))
def c_jump(a):
    need(a, 1, "jump <bar>")
    out(call("jump", time=bar_to_beats(a[0])))


@command("tempo", "tempo [bpm]", "show or set the tempo", "transport")
def c_tempo(a):
    if a:
        return out(call("set_song", tempo=float(a[0]))["tempo"])
    out(call("song")["tempo"])


@command("signature", "signature <n/d>", "time signature, e.g. 7/8", "transport")
def c_signature(a):
    need(a, 1, "signature <n/d>")
    out(call("set_song", signature=a[0])["signature"])


@command("metronome", "metronome on|off", "metronome", "transport")
def c_metronome(a):
    need(a, 1, "metronome on|off")
    out(call("set_song", metronome=a[0] == "on")["metronome"])


@command("loop", "loop on|off [start-bar length-bars]", "arrangement loop", "transport")
def c_loop(a):
    need(a, 1, "loop on|off [start-bar length-bars]")
    kw = {"loop": a[0] == "on"}
    if len(a) >= 3:
        s = call("song")
        n, d = [int(x) for x in s["signature"].split("/")]
        kw.update(loop_start=bar_to_beats(a[1], s), loop_length=float(a[2]) * n * 4.0 / d)
    r = call("set_song", **kw)
    out(r, "loop %s, start %g beats, length %g beats" % ("on" if r["loop"] else "off", r["loop_start"], r["loop_length"]))


@command("tap", "tap", "tap tempo", "transport")
def c_tap(a):
    out(call("tap_tempo"))


@command("capture-midi", "capture-midi", "Live's Capture MIDI", "transport")
def c_capture_midi(a):
    out(call("capture_midi"), "captured")


@command("locator", "locator <bar> <name>", "create or rename a locator (stops playback for a moment)", "transport", aliases=("locatore",))
def c_locator(a):
    need(a, 2, "locator <bar> <name>")
    r = call("locator_set", time=bar_to_beats(a[0]), name=" ".join(a[1:]), timeout=10)
    out(r, "locator at bar %s: %s" % (a[0], " ".join(a[1:])))


@command("locator-delete", "locator-delete <bar>", "delete a locator", "transport")
def c_locator_delete(a):
    need(a, 1, "locator-delete <bar>")
    out(call("locator_delete", time=bar_to_beats(a[0])), "deleted")


# ================================================================== tracks and mixer
@command("track-new", "track-new audio|midi|return [name] [index]", "create a track", "tracks")
def c_track_new(a):
    need(a, 1, "track-new audio|midi|return [name] [index]")
    out(call("track_create", kind=a[0], name=a[1] if len(a) > 1 else None, index=int(a[2]) if len(a) > 2 else -1))


@command("track-delete", "track-delete <track>", "delete a track", "tracks")
def c_track_delete(a):
    need(a, 1, "track-delete <track>")
    out(call("track_delete", track=track(a[0])["ref"]))


@command("track-duplicate", "track-duplicate <track>", "duplicate a track", "tracks")
def c_track_duplicate(a):
    need(a, 1, "track-duplicate <track>")
    out(call("track_duplicate", track=track(a[0])["ref"]))


@command("track-rename", "track-rename <track> <name>", "rename a track", "tracks")
def c_track_rename(a):
    need(a, 2, "track-rename <track> <name>")
    out(call("track_set", track=track(a[0])["ref"], name=" ".join(a[1:])))


@command("track-color", "track-color <track> <#RRGGBB>", "track color", "tracks")
def c_track_color(a):
    need(a, 2, "track-color <track> <#RRGGBB>")
    out(call("track_set", track=track(a[0])["ref"], color=a[1]))


@command("monitor", "monitor <track> in|auto|off", "monitoring state", "tracks")
def c_monitor(a):
    need(a, 2, "monitor <track> in|auto|off")
    out(call("track_set", track=track(a[0])["ref"], monitor=a[1]))


def _toggle(key):
    def f(a):
        need(a, 2, "%s <track> on|off" % key)
        t = track(a[0])
        r = call("track_set", track=t["ref"], **{key: a[1] == "on"})
        out(r, "%s: %s %s" % (t["name"], key, "on" if r.get(key) else "off"))
    return f


command("solo", "solo <track> on|off", "solo", "tracks")(_toggle("solo"))
command("mute", "mute <track> on|off", "mute", "tracks")(_toggle("mute"))
command("arm", "arm <track> on|off", "record arm", "tracks")(_toggle("arm"))


def _pan_value(v):
    """'20L' -> -0.4, 'C' -> 0, '35R' -> 0.7 (Live's panning: -1 = 50L, 1 = 50R)."""
    t = v.strip().upper()
    if t in ("C", "0", "CENTER", "CENTRE"):
        return 0.0
    n = parse_number(t)
    if n is None or not (t.endswith("L") or t.endswith("R")):
        raise LiveError("pan: use 20L, C or 35R (or =raw -1..1)")
    return max(-1.0, min(1.0, (-n if t.endswith("L") else n) / 50.0))


def _mixer_value(kind, v):
    if v.startswith("="):
        return {"value": float(v[1:])}
    if kind == "pan":
        return {"value": _pan_value(v)}
    if kind in ("volume", "send") and not v.lower().endswith("db") and parse_number(v) is not None:
        return {"text": v + " dB"}
    return {"text": v}


@command("volume", "volume <track> <dB|=raw>", "e.g. live volume t3 -6", "tracks")
def c_volume(a):
    need(a, 2, "volume <track> <dB|=raw>")
    out(call("set_mixer", track=track(a[0])["ref"], what="volume", **_mixer_value("volume", a[1])))


@command("pan", "pan <track> <20L|C|35R|=raw>", "panning", "tracks")
def c_pan(a):
    need(a, 2, "pan <track> <value>")
    out(call("set_mixer", track=track(a[0])["ref"], what="pan", **_mixer_value("pan", a[1])))


@command("send", "send <track> <return letter|index> <dB|=raw>", "send level", "tracks", aliases=("mandata",))
def c_send(a):
    need(a, 3, "send <track> <return> <dB>")
    idx = ord(a[1].lower()) - 97 if a[1].isalpha() else int(a[1])
    out(call("set_mixer", track=track(a[0])["ref"], what="send", index=idx, **_mixer_value("send", a[2])))


# ================================================================== devices
def set_plugin_param(ref, path, idx, text):
    """Plugins can't translate a value to text in advance: set, wait, read back (binary search)."""
    goal = parse_number(text)
    if goal is None:
        raise LiveError("not a number: %s" % text)

    def probe(v):
        call("set_param", track=ref, device=path, param=idx, value=v)
        time.sleep(0.12)
        p = call("params", track=ref, device=path)["params"][idx]
        return parse_number(p["text"]), p

    lo_v, _ = probe(0.0)
    hi_v, p = probe(1.0)
    if lo_v is None or hi_v is None:
        raise LiveError("this parameter has no readable numeric values")
    up = hi_v >= lo_v
    lo, hi = 0.0, 1.0
    for _ in range(14):
        mid = (lo + hi) / 2
        v, p = probe(mid)
        if v is None:
            break
        if abs(v - goal) <= max(abs(goal) * 0.005, 0.01):
            return p
        if (v < goal) == up:
            lo = mid
        else:
            hi = mid
    return probe((lo + hi) / 2)[1]


@command("set", "set <track> <device> <param> <value|=raw>", "set a parameter by its display value ('-3 dB', '250 Hz', "
         "'Bell') or raw (=0.5)", "devices", aliases=("imposta",))
def c_set(a):
    need(a, 4, "set <track> <device> <param> <value>")
    t = track(a[0])
    d = find_device(t, a[1])
    info = call("params", track=t["ref"], device=d["path"])
    p = find_param(info["params"], a[2])
    v = " ".join(a[3:])
    before = p["text"]
    if info["class"] in ("AuPluginDevice", "PluginDevice") and not v.startswith("=") and not p["quantized"]:
        p2 = set_plugin_param(t["ref"], d["path"], p["i"], v)
        after = p2["text"]
    else:
        kw = {"value": float(v[1:])} if v.startswith("=") else {"text": v}
        call("set_param", track=t["ref"], device=d["path"], param=p["i"], **kw)
        time.sleep(0.1)
        after = call("params", track=t["ref"], device=d["path"])["params"][p["i"]]["text"]
    out({"track": t["name"], "device": info["device"], "param": p["name"], "before": before, "after": after},
        "%s / %s / %s: %s -> %s" % (t["name"], info["device"], p["name"], before, after))


def _on(on):
    def f(a):
        need(a, 2, "%s <track> <device>" % ("on" if on else "off"))
        t = track(a[0])
        d = find_device(t, a[1])
        r = call("device_on", track=t["ref"], device=d["path"], on=on)
        out(r, "%s / %s: %s -> %s" % (t["name"], r["device"], onoff(r["before"]).strip(), onoff(r["after"]).strip()))
    return f


command("on", "on <track> <device>", "switch a device on", "devices", aliases=("accendi",))(_on(True))
command("off", "off <track> <device>", "switch a device off", "devices", aliases=("spegni",))(_on(False))


@command("load", "load <track> <browser path> [after-device]", "load a browser item (path from 'live search')", "devices", aliases=("carica",))
def c_load(a):
    need(a, 2, "load <track> <browser path> [after-device]")
    t = track(a[0])
    after = find_device(t, a[2])["path"] if len(a) > 2 else None
    r = call("load", track=t["ref"], path=a[1], after=after, timeout=30)
    out(r, "loaded %s on %s: %s" % (r["loaded"], t["name"], ", ".join(r["devices"])))


@command("delete", "delete <track> <device>", "delete a device", "devices", aliases=("cancella",))
def c_delete(a):
    need(a, 2, "delete <track> <device>")
    t = track(a[0])
    d = find_device(t, a[1])
    r = call("delete_device", track=t["ref"], device=d["path"])
    out(r, "deleted %s from %s: %s" % (r["deleted"], t["name"], ", ".join(r["devices"])))


@command("move", "move <track> <device> <position>", "move a device inside its chain", "devices", aliases=("sposta",))
def c_move(a):
    need(a, 3, "move <track> <device> <position>")
    t = track(a[0])
    d = find_device(t, a[1])
    r = call("move_device", track=t["ref"], device=d["path"], position=int(a[2]))
    out(r, "moved %s: %s" % (r["moved"], ", ".join(r["devices"])))


@command("rename", "rename <track> <device> <name>", "rename a device", "devices")
def c_rename(a):
    need(a, 3, "rename <track> <device> <name>")
    t = track(a[0])
    out(call("rename_device", track=t["ref"], device=find_device(t, a[1])["path"], name=" ".join(a[2:])))


def replace(triplets):
    """Replace devices with browser items, safely with hardware inserts:
    1) switch OFF every device to replace (an External Audio Effect that is off neither sends nor receives,
       so a new preset using the same I/O can't create a feedback loop through the hardware),
    2) for each one: load the new item right after it, check positions, delete the old one."""
    ts = tracks()
    jobs = []
    for tq, dq, path in triplets:
        t = find_track(ts, tq)
        d = find_device(t, dq)
        if "." in d["path"]:
            raise LiveError("replace works on top-level devices (%s is inside a rack)" % d["name"])
        jobs.append((t["ref"], t["name"], int(d["path"]), d["name"], path))
    call("undo_begin")
    try:
        _replace_jobs(jobs)
    finally:
        call("undo_end")


def _replace_jobs(jobs):
    for ref, tn, i, name, _ in jobs:
        call("device_on", track=ref, device=str(i), on=False)
        print("off      %s / %s" % (tn, name))
    time.sleep(0.2)
    for ref, tn, i, old, path in jobs:
        before = [x["name"] for x in find_track(tracks(), ref)["devices"]]
        if before[i] != old:
            raise LiveError("%s: position %d now holds %s, not %s. Stopping." % (tn, i, before[i], old))
        r = call("load", track=ref, path=path, after=str(i), timeout=30)
        time.sleep(0.3)
        after = [x["name"] for x in find_track(tracks(), ref)["devices"]]
        if len(after) != len(before) + 1:
            raise LiveError("load did not work as expected on %s: before %s, after %s" % (tn, before, after))
        new_i = next((k for k in range(len(after)) if k >= len(before) or after[k] != before[k]), len(after) - 1)
        if new_i != i + 1:
            call("move_device", track=ref, device=str(new_i), position=i + 1)
            time.sleep(0.2)
        cur = [x["name"] for x in find_track(tracks(), ref)["devices"]]
        if cur[i] != old:
            raise LiveError("check failed on %s: position %d holds %s, not %s. Nothing deleted. Chain: %s" % (tn, i, cur[i], old, cur))
        call("delete_device", track=ref, device=str(i))
        time.sleep(0.2)
        print("%s: %s -> %s" % (tn, old, r["loaded"]))


@command("replace", "replace <track> <device> <browser path> [...]", "replace devices (several triplets at once); "
         "all old devices are switched off first", "devices", aliases=("sostituisci",))
def c_replace(a):
    if len(a) < 3 or len(a) % 3:
        raise LiveError("usage: live replace <track> <device> <path> [<track> <device> <path> ...]")
    replace([tuple(a[k:k + 3]) for k in range(0, len(a), 3)])


@command("mode", "mode <name>|restore", "switch off the devices listed in config 'modes' (e.g. heavy plugins while "
         "recording); 'restore' switches them back on", "devices", aliases=("modo",))
def c_mode(a):
    need(a, 1, "mode <name>|restore")
    modes = config().get("modes", {})
    state = os.path.join(CONFIG_DIR, "mode-state.json")
    if a[0] in ("restore", "mix"):
        if not os.path.exists(state):
            raise LiveError("no saved mode to restore")
        ts = tracks()
        for s in json.load(open(state)):
            t = next((x for x in ts if x["ref"] == s["track"] and x["name"] == s["track_name"]), None)
            d = next((x for x in all_devices(t["devices"]) if x["path"] == s["path"]), None) if t else None
            if d is None or d["name"] != s["name"]:
                print("skipped (set changed): %s / %s" % (s["track_name"], s["name"]))
                continue
            call("device_on", track=s["track"], device=s["path"], on=True)
            print("on   %-24s %s" % (s["track_name"][:24], s["name"]))
        os.remove(state)
        return
    names = modes.get(a[0]) or (modes.get("registrazione") if a[0] == "recording" else None)
    if not names:
        raise LiveError("mode '%s' not in config (modes: %s)" % (a[0], ", ".join(modes) or "none"))
    names = [n.lower() for n in names]
    done = []
    call("undo_begin")
    try:
        for t in tracks():
            for d in all_devices(t["devices"]):
                if d["on"] and any(n in d["name"].lower() for n in names):
                    call("device_on", track=t["ref"], device=d["path"], on=False)
                    done.append({"track": t["ref"], "track_name": t["name"], "path": d["path"], "name": d["name"]})
                    print("off  %-24s %s" % (t["name"][:24], d["name"]))
    finally:
        call("undo_end")
    os.makedirs(CONFIG_DIR, exist_ok=True)
    json.dump(done, open(state, "w"), indent=1, ensure_ascii=False)
    print("%d devices off; 'live mode restore' switches them back on" % len(done))


# ================================================================== scenes and clips
@command("scene", "scene fire|new|rename|delete|duplicate <index> [name]", "session scenes", "clips")
def c_scene(a):
    need(a, 1, "scene fire|new|rename|delete|duplicate <index> [name]")
    op = a[0]
    if op == "fire":
        return out(call("scene_fire", scene=int(a[1])))
    if op == "new":
        return out(call("scene_create", index=int(a[1]) if len(a) > 1 else -1, name=" ".join(a[2:]) or None))
    if op == "rename":
        return out(call("scene_set", scene=int(a[1]), name=" ".join(a[2:])))
    if op == "delete":
        return out(call("scene_delete", scene=int(a[1])))
    if op == "duplicate":
        return out(call("scene_duplicate", scene=int(a[1])))
    raise LiveError("scene fire|new|rename|delete|duplicate")


@command("clip", "clip fire|stop|new|delete|duplicate|rename <track> <slot> [length|name]", "session clips", "clips")
def c_clip(a):
    need(a, 2, "clip fire|stop|new|delete|duplicate|rename <track> <slot> [length|name]")
    op, ref = a[0], track(a[1])["ref"]
    if len(a) > 2 and a[2].startswith("@"):
        cr = clip_ref(a[2])
        if op == "delete":
            return out(call("clip_delete", track=ref, **cr))
        if op == "rename":
            return out(call("clip_set", track=ref, name=" ".join(a[3:]), **cr))
        raise LiveError("arrangement clips (@N) support: delete, rename (and clip-set, clip-edit)")
    slot = int(a[2]) if len(a) > 2 else None
    if op == "fire":
        return out(call("clip_fire", track=ref, slot=slot))
    if op == "stop":
        return out(call("clip_stop", track=ref, slot=slot))
    if op == "new":
        return out(call("clip_create", track=ref, slot=slot, length=float(a[3]) if len(a) > 3 else 4.0))
    if op == "delete":
        return out(call("clip_delete", track=ref, slot=slot))
    if op == "duplicate":
        return out(call("clip_duplicate", track=ref, slot=slot))
    if op == "rename":
        return out(call("clip_set", track=ref, slot=slot, name=" ".join(a[3:])))
    raise LiveError("clip fire|stop|new|delete|duplicate|rename")


@command("set-notes", "set-notes <track> <slot|@arr> [--add] < notes.json", "write MIDI notes from stdin: "
         "[{\"pitch\":60,\"start\":0,\"duration\":0.5,\"velocity\":100}, ...]", "clips")
def c_set_notes(a):
    need(a, 2, "set-notes <track> <slot> [--add] < notes.json")
    notes = json.load(sys.stdin)
    out(call("set_notes", track=track(a[0])["ref"], notes=notes, add="--add" in a, **clip_ref(a[1])))


# ================================================================== audio capture
@command("capture", "capture <seconds> <file.wav>", "record the configured input device through Live (see README)", "audio", aliases=("cattura",))
def c_capture(a):
    need(a, 2, "capture <seconds> <file.wav>")
    sec, path = float(a[0]), os.path.abspath(a[1])
    r = call("capture", seconds=sec, file=path)
    t0 = time.time()
    while time.time() - t0 < sec + 15:
        try:
            os.kill(r["pid"], 0)
        except OSError:
            break
        time.sleep(0.5)
    if not os.path.exists(path) or os.path.getsize(path) < 1000:
        log = path + ".log"
        raise LiveError("capture failed: %s" % (open(log).read().strip() if os.path.exists(log) else "no file"))
    out({"file": path, "seconds": sec}, path)


# ================================================================== bounce (record inside Live)
@command("bounce", "bounce <track|master> <start-bar> <bars> [name]", "record a track's output (Post Mixer) or the master "
         "(Resampling) into a new audio track in the arrangement, in real time: Live's API has no export, this is the "
         "next best thing. Prints the recorded file", "audio")
def c_bounce(a):
    need(a, 3, "bounce <track|master> <start-bar> <bars> [name]")
    src_q, start_bar, bars = a[0], float(a[1]), float(a[2])
    s_ = call("song")
    if s_["playing"]:
        raise LiveError("stop playback first")
    ts = tracks()
    if src_q.lower() in ("m", "master", "main"):
        src_type, src_ch, src_name = "Resampling", None, "Master"
    else:
        t = find_track(ts, src_q)
        if t["ref"][0] != "t":
            raise LiveError("source must be a regular track or the master")
        src_type, src_ch, src_name = t["name"], "Post Mixer", t["name"]
    name = " ".join(a[3:]) or "Bounce %s" % src_name
    n, d = [int(x) for x in s_["signature"].split("/")]
    beats_per_bar = n * 4.0 / d
    start, end = (start_bar - 1) * beats_per_bar, (start_bar - 1 + bars) * beats_per_bar
    armed = [t["ref"] for t in ts if t.get("arm")]
    selected_before = None
    new = call("track_create", kind="audio", name=name, index=-1)
    ref = new["ref"]
    old = {k: s_[k] for k in ("position", "loop", "loop_start", "loop_length", "punch_in", "punch_out")}
    preroll = beats_per_bar
    try:
        call("set_routing", track=ref, side="input", type=src_type, channel=src_ch)
        call("track_set", track=ref, monitor="off")
    except LiveError:
        call("track_delete", track=ref)      # don't leave an empty track behind
        raise
    try:
        for r_ in armed:
            call("track_set", track=r_, arm=False)
        # select the bounce track: Live's automatic arm follows the selected MIDI track, and a track that is
        # armed in any way would be recorded too (overwriting its clips in the punch region)
        call("select", track=ref)
        call("track_set", track=ref, arm=True)
        time.sleep(0.2)
        still = [t_["name"] for t_ in tracks() if t_["ref"] != ref and (t_.get("arm") or t_.get("implicit_arm"))]
        if still:
            for r_ in armed:
                call("track_set", track=r_, arm=True)
            call("track_delete", track=ref)
            ref = None
            raise LiveError("not recording: these tracks are still armed and would be recorded too: %s" % ", ".join(still))
        # punch in/out on the loop region: Live itself records exactly from start to end
        # (punch uses the loop brace; the loop switch stays off so playback doesn't jump back)
        call("set_song", loop=False, loop_start=start, loop_length=end - start, punch_in=True, punch_out=True)
        call("play")
        call("jump", time=max(0.0, start - preroll))
        call("record", on=True)
        sec_per_beat = 60.0 / s_["tempo"]
        deadline = time.time() + (end - start + preroll * 2) * sec_per_beat + 10
        print("recording %s from bar %g for %g bars..." % (src_name, start_bar, bars), file=sys.stderr)
        prev = None
        while time.time() < deadline:
            pos = call("song")["position"]
            if end + 0.25 <= pos or (prev is not None and pos < prev - 0.5):    # past the end, or jumped back
                break
            prev = pos
            time.sleep(0.1)
    finally:
        call("stop")
        call("record", on=False)
        if ref is not None:
            call("track_set", track=ref, arm=False)
            for r_ in armed:
                call("track_set", track=r_, arm=True)
        call("set_song", **old)
    time.sleep(0.5)
    clips = call("arrangement", track=ref)
    info = {"track": ref, "name": name, "source": src_name, "start_bar": start_bar, "bars": bars, "clips": clips,
            "file": clips[-1].get("file") if clips else None}
    if len(clips) == 1:
        c = call("clip_set", track=ref, arr=0)
        spb = 60.0 / s_["tempo"]          # recorded at the song tempo
        # the clip's beat 0 is not the file's first sample: the warp markers map clip beats to file seconds
        wm = call("warp_markers", track=ref, arr=0)
        if len(wm) >= 2 and wm[1]["beat"] != wm[0]["beat"]:
            spb_file = (wm[1]["sample_time"] - wm[0]["sample_time"]) / (wm[1]["beat"] - wm[0]["beat"])
        else:
            spb_file = spb
        b0, t0 = (wm[0]["beat"], wm[0]["sample_time"]) if wm else (0.0, 0.0)
        info["file_offset_s"] = round(t0 + (c["start_marker"] - b0) * spb_file, 6)
        info["duration_s"] = round((c["end_marker"] - c["start_marker"]) * spb_file, 6)
    text = "bounced to %s (%s): %s" % (ref, name, info["file"] or "no clip recorded")
    if "file_offset_s" in info:
        text += "\n  the region is %.3f s long and starts %.3f s into the file (the rest is pre-roll): ffmpeg -ss %.3f -t %.3f" % (
            info["duration_s"], info["file_offset_s"], info["file_offset_s"], info["duration_s"])
    out(info, text)


# ================================================================== probes
def _probe_pairs():
    srvs = P.servers()
    if not srvs:
        raise LiveError("no probe servers found in %s: is a probe plugin loaded in Live?" % P.SOCKET_DIR)
    return srvs, P.instances(srvs)


def _probe_devices():
    """Map probe_id -> (track ref, track name, device path) by reading 'Probe ID' params in Live."""
    names = [n.lower() for n in config().get("probe_plugins", ["Loudline Probe", "Reference Probe", "Probe"])]
    out_ = {}
    try:
        ts = tracks()
    except LiveError:
        return out_
    for t in ts:
        for d in all_devices(t["devices"]):
            if any(n in d["name"].lower() for n in names):
                try:
                    ps = call("params", track=t["ref"], device=d["path"])["params"]
                    p = next(p for p in ps if p["name"].lower().startswith("probe id"))
                    pid = int(round(p["value"] * 999)) if p["max"] <= 1.0 else int(round(p["value"]))
                    out_[pid] = (t["ref"], t["name"], d["path"])
                except (StopIteration, LiveError):
                    pass
    return out_


@command("probes", "probes", "probe instances found (track, id, label, signal)", "probes", aliases=("sonde",))
def c_probes(a):
    srvs, insts = _probe_pairs()
    devs = _probe_devices()
    rows = []
    for s, i in insts:
        d = devs.get(i.get("probe_id"))
        rows.append(dict(i, live_track=d[0] if d else None, live_device=d[2] if d else None))
    if JSON:
        return out({"servers": [s.info for s in srvs], "probes": rows})
    for s in srvs:
        print("%s %s (protocol %s, pid %s): %s" % (s.info.get("product"), s.info.get("version"), s.info.get("protocol"),
              s.info.get("pid"), ", ".join(s.info.get("capabilities", []))))
    for r in rows:
        where = " @ %s[%s]" % (r["live_track"], r["live_device"]) if r.get("live_track") else ""
        print("  #%-3s %-26s %-18s %s%s" % (r.get("probe_id"), (r.get("track") or "?")[:26], (r.get("label") or "")[:18],
              "signal" if r.get("signal") else ("active" if r.get("active") else "idle"), where))


@command("probe-add", "probe-add <track> [after <device>|first] [label] [--plugin NAME]", "load a probe plugin on a track, give it a new "
         "Probe ID and wait until it reports", "probes", aliases=("sonda",))
def c_probe_add(a):
    need(a, 1, "probe-add <track> [after <device>|first] [label]")
    t = track(a[0])
    rest = a[1:]
    after, first = None, False
    if rest[:1] == ["after"] and len(rest) > 1:
        after = find_device(t, rest[1])["path"]
        if "." in after:
            raise LiveError("probe-add works on top-level devices (%s is inside a rack)" % rest[1])
        rest = rest[2:]
    elif rest[:1] == ["first"]:
        first, rest = True, rest[1:]
    plugin = config().get("probe_plugin", "Loudline Probe")
    if "--plugin" in rest:
        k = rest.index("--plugin")
        plugin = rest[k + 1]
        rest = rest[:k] + rest[k + 2:]
    label = " ".join(rest)
    path = None if "--plugin" in a else config().get("probe_browser_path")
    if not path:
        hits = [h for h in call("search", q=plugin, roots=["plugins"], timeout=30) if h.lower().endswith(plugin.lower())]
        if not hits:
            raise LiveError("probe plugin '%s' not found in the browser (installed? rescanned?)" % plugin)
        path = sorted(hits, key=lambda h: ("vst3" not in h.lower(), h))[0]
    used = set(_probe_devices())
    try:
        used |= {i.get("probe_id") for _, i in P.instances()}
    except LiveError:
        pass
    pid = next(n for n in range(1, 1000) if n not in used)
    before = [d["name"] for d in track(t["ref"])["devices"]]
    call("load", track=t["ref"], path=path, after=after, timeout=30)
    time.sleep(0.4)
    now = [d["name"] for d in track(t["ref"])["devices"]]
    if len(now) != len(before) + 1:
        raise LiveError("probe not loaded as expected: %s" % now)
    new_i = next((k for k in range(len(now)) if k >= len(before) or now[k] != before[k]), len(now) - 1)
    want = 0 if first else (int(after) + 1 if after is not None else len(now) - 1)
    if new_i != want:
        call("move_device", track=t["ref"], device=str(new_i), position=want)
        new_i = want
        time.sleep(0.2)
    ps = call("params", track=t["ref"], device=str(new_i))["params"]
    p = next((p for p in ps if p["name"].lower().startswith("probe id")), None)
    if p is None:
        raise LiveError("the loaded device has no 'Probe ID' parameter: %s" % [x["name"] for x in ps])
    call("set_param", track=t["ref"], device=str(new_i), param=p["i"], value=pid / 999.0 if p["max"] <= 1.0 else pid)
    deadline = time.time() + 5
    inst = None
    while time.time() < deadline:
        try:
            inst = next(((s, i) for s, i in P.instances() if i.get("probe_id") == pid), None)
        except LiveError:
            inst = None
        if inst:
            break
        time.sleep(0.3)
    if not inst:
        raise LiveError("probe loaded (device %s) but it does not report on a socket yet" % new_i)
    if label:
        inst[0].request({"cmd": "set_label", "uid": inst[1]["uid"], "label": label})
    out({"probe_id": pid, "track": t["name"], "device": str(new_i), "uid": inst[1]["uid"], "label": label},
        "probe #%d on %s (device %d)%s" % (pid, t["name"], new_i, " [%s]" % label if label else ""))


def _fmt_meas(m):
    parts = []
    for k, f in (("lufs_s", "LUFS-S %.1f"), ("lufs_i", "LUFS-I %.1f"), ("true_peak_dbtp", "TP %.1f"), ("peak_db", "peak %.1f"),
                 ("rms_db", "RMS %.1f"), ("plr", "PLR %.1f"), ("crest_db", "crest %.1f"), ("lra", "LRA %.1f"),
                 ("correlation", "corr %.2f"), ("width", "width %.2f"), ("bass_mono", "bass mono %.2f")):
        v = m.get(k)
        if isinstance(v, list):
            v = max(v)
        if v is not None:
            parts.append(f % v)
    return "  ".join(parts)


def _fmt_bands(bands, ref=None):
    lines = []
    top = max(v for v in bands if v is not None)
    for c, v in zip(P.CENTERS, bands):
        if v is None:
            continue
        bar = "#" * max(0, int((v - top + 60) / 2))
        lines.append("  %6s Hz %7.1f dB %s" % (P.fmt_hz(c), v, bar))
    return "\n".join(lines)


@command("measure", "measure <probe> [seconds] [--bands]", "average levels of a probe over N seconds (default 3)", "probes", aliases=("misura",))
def c_measure(a):
    need(a, 1, "measure <probe> [seconds] [--bands]")
    _, insts = _probe_pairs()
    pair = P.find(insts, a[0])
    sec = float(a[1]) if len(a) > 1 and not a[1].startswith("--") else 3.0
    m = P.collect([pair], sec)[pair[1]["uid"]]
    if JSON:
        return out({"probe": pair[1], "seconds": sec, "measure": m})
    print("%s, %gs: %s" % (P.describe(pair[1]), sec, _fmt_meas(m)))
    if m.get("bands_mid"):
        z = P.zone_energy(m["bands_mid"])
        print("  zones: " + "  ".join("%s %.1f" % (k, v) for k, v in z.items() if v is not None))
        if "--bands" in a:
            print(_fmt_bands(m["bands_mid"]))


@command("watch", "watch <probe> [seconds] [hz]", "stream a probe's levels (default 10 s at 4 Hz)", "probes")
def c_watch(a):
    need(a, 1, "watch <probe> [seconds] [hz]")
    _, insts = _probe_pairs()
    s, i = P.find(insts, a[0])
    sec = float(a[1]) if len(a) > 1 else 10
    hz = float(a[2]) if len(a) > 2 else 4
    t_end = time.time() + sec
    if s.has("subscribe"):
        s.request({"cmd": "subscribe", "uid": i["uid"], "hz": hz})
        while time.time() < t_end:
            try:
                ev = s.readline(timeout=2)
            except OSError:
                print("(no data for 2 s)")
                continue
            if ev.get("event") == "snapshot" and ev.get("uid") == i["uid"]:
                print(json.dumps(ev["snapshot"]) if JSON else _fmt_meas(ev["snapshot"]))
                sys.stdout.flush()
        s.request({"cmd": "unsubscribe", "uid": i["uid"]})
    else:
        while time.time() < t_end:
            snap = s.request({"cmd": "get", "uid": i["uid"]})["snapshot"]
            print(json.dumps(snap) if JSON else _fmt_meas(snap))
            sys.stdout.flush()
            time.sleep(1.0 / hz)


@command("compare", "compare <probe-a> <probe-b> [seconds] [--bands] [--transfer] [--live]", "what happens between two probes "
         "(e.g. before/after a plugin): gain per 1/3-octave band and loudness change", "probes", aliases=("confronta",))
def c_compare(a):
    need(a, 2, "compare <probe-a> <probe-b> [seconds]")
    _, insts = _probe_pairs()
    pa, pb = P.find(insts, a[0]), P.find(insts, a[1])
    nums = [x for x in a[2:] if not x.startswith("--")]
    sec = float(nums[0]) if nums else 10.0
    r = P.compare(pa, pb, sec, transfer="--transfer" in a, live="--live" in a)
    if JSON:
        return out(dict(r, a_probe=pa[1], b_probe=pb[1]))
    print("%s -> %s, %gs (%s)" % (P.describe(pa[1]), P.describe(pb[1]), r["seconds"], r["method"]))
    if r.get("loudness_gain_db") is not None:
        print("  overall: %+.1f dB" % r["loudness_gain_db"])
    if r.get("a") and r.get("b"):
        print("  A: " + _fmt_meas(r["a"]))
        print("  B: " + _fmt_meas(r["b"]))
    g = r["gain_db"]
    valid = [x for x in g if x is not None]
    base = statistics.median(valid) if valid else 0
    regions = P.regions(r["centers"], g, base)
    print("  broadband %+.1f dB (median of the bands)" % base)
    if not regions:
        print("  no band differs from it by 1 dB or more")
    for reg in regions:
        print("  %s %+.1f dB at %s Hz (%s-%s Hz)" % ("boost" if reg["db"] > 0 else "cut  ", reg["db"], P.fmt_hz(reg["at"]),
              P.fmt_hz(reg["lo"]), P.fmt_hz(reg["hi"])))
    if "--bands" in a:
        for c, v in zip(r["centers"], g):
            if v is None:
                print("  %6s Hz      -   (no signal in A)" % P.fmt_hz(c))
                continue
            d = v - base
            bar = ("+" * min(30, int(round(d * 2)))) if d > 0 else ("-" * min(30, int(round(-d * 2))))
            print("  %6s Hz %+6.1f dB %s" % (P.fmt_hz(c), v, bar))
    if r.get("transfer"):
        tr = r["transfer"]
        print("  transfer: delay %s samples, %d points (see --json)" % (tr.get("delay_samples"), len(tr.get("freqs", []))))


@command("mix", "mix [seconds]", "all probes together: levels and which probes crowd each frequency zone", "probes")
def c_mix(a):
    sec = float(a[0]) if a else 5.0
    _, insts = _probe_pairs()
    insts = [x for x in insts if x[1].get("active", True)]
    meas = P.collect(insts, sec)
    rows = []
    for s, i in insts:
        m = meas[i["uid"]]
        rows.append({"probe": i, "measure": m, "zones": P.zone_energy(m["bands_mid"]) if m.get("bands_mid") else {}})
    crowd = {}
    for name, _, _ in P.ZONES:
        vals = [(r["zones"].get(name), r) for r in rows if r["zones"].get(name) is not None and r["zones"][name] > -80]
        if not vals:
            continue
        top = max(v for v, _ in vals)
        crowd[name] = [P.describe(r["probe"]) for v, r in vals if v >= top - 6]
    if JSON:
        return out({"seconds": sec, "probes": rows, "crowded": crowd})
    print("mix over %gs, %d probes" % (sec, len(rows)))
    for r in sorted(rows, key=lambda r: -(r["measure"].get("lufs_s") or r["measure"].get("rms_db") or -200)):
        print("  %-34s %s" % (P.describe(r["probe"])[:34], _fmt_meas(r["measure"])))
    print("probes within 6 dB of the loudest, per zone:")
    for z, names in crowd.items():
        lo, hi = next((l, h) for n, l, h in P.ZONES if n == z)
        print("  %-9s %5s-%-5s Hz  %d: %s" % (z, P.fmt_hz(lo), P.fmt_hz(hi), len(names), ", ".join(names)))


@command("probe-reset", "probe-reset [probe]", "reset integrated loudness, max true peak and history (one or all)", "probes")
def c_probe_reset(a):
    srvs, insts = _probe_pairs()
    if a:
        s, i = P.find(insts, a[0])
        s.request({"cmd": "reset", "uid": i["uid"]})
    else:
        for s in srvs:
            s.request({"cmd": "reset", "all": True})
    out(True, "reset")


@command("probe-label", "probe-label <probe> <label>", "label a probe (not saved with the project)", "probes")
def c_probe_label(a):
    need(a, 2, "probe-label <probe> <label>")
    _, insts = _probe_pairs()
    s, i = P.find(insts, a[0])
    s.request({"cmd": "set_label", "uid": i["uid"], "label": " ".join(a[1:])})
    out(True, "ok")


@command("inspect", "inspect song|app|view|track <t>|mixer <t>|device <t> <d>|param <t> <d> <p>|clip <t> <slot>|scene <i> [filter]",
         "what Live's API offers on an object (properties with values, methods): read-only", "other")
def c_inspect(a):
    what = a[0] if a else "song"
    rest = a[1:]
    kw = {"what": what}
    if what in ("track", "mixer", "device", "param", "clip", "arrangement_clip"):
        need(rest, 1, "inspect %s <track> ..." % what)
        t = track(rest[0])
        kw["track"] = t["ref"]
        rest = rest[1:]
        if what in ("device", "param"):
            need(rest, 1, "inspect %s <track> <device>" % what)
            kw["device"] = find_device(t, rest[0])["path"]
            rest = rest[1:]
            if what == "param":
                need(rest, 1, "inspect param <track> <device> <param>")
                ps = call("params", track=t["ref"], device=kw["device"])["params"]
                kw["param"] = find_param(ps, rest[0])["i"]
                rest = rest[1:]
        if what == "clip":
            kw["slot"] = int(rest[0])
            rest = rest[1:]
        if what == "arrangement_clip":
            kw["index"] = int(rest[0])
            rest = rest[1:]
    elif what == "scene":
        kw["scene"] = int(rest[0])
        rest = rest[1:]
    f = rest[0].lower() if rest else ""
    r = call("inspect", timeout=15, **kw)
    if f:
        r["properties"] = {k: v for k, v in r["properties"].items() if f in k.lower()}
        r["methods"] = [m for m in r["methods"] if f in m.lower()]
    if JSON:
        return out(r)
    print("%s (%d properties, %d methods)" % (r["type"], len(r["properties"]), len(r["methods"])))
    for k, v in r["properties"].items():
        print("  %-40s %s" % (k, v))
    if r["methods"]:
        print("  methods: " + ", ".join(r["methods"]))


@command("ab", "ab <track> <device> [a|b|toggle|save]", "Live 12 device A/B compare: show, switch, or copy the "
         "current state into the other slot", "devices")
def c_ab(a):
    need(a, 2, "ab <track> <device> [a|b|toggle|save]")
    t = track(a[0])
    d = find_device(t, a[1])
    r = call("ab", track=t["ref"], device=d["path"], op=a[2] if len(a) > 2 else None)
    out(r, "%s / %s: using %s" % (t["name"], r["device"], r["using"]))


@command("impact", "impact [n]", "tracks sorted by Live's CPU impact (performance_impact)", "info")
def c_impact(a):
    n = int(a[0]) if a else 15
    ts = sorted(tracks(), key=lambda t: -(t.get("cpu_impact") or 0))[:n]
    rows = [{"ref": t["ref"], "name": t["name"], "cpu_impact": t.get("cpu_impact")} for t in ts]
    out(rows, "\n".join("  %-4s %-28s %5.1f%%  %s" % (r["ref"], r["name"][:28], 100 * (r["cpu_impact"] or 0),
                                                     "#" * int(200 * (r["cpu_impact"] or 0))) for r in rows))


def _find_probe_at(ref, path):
    devs = _probe_devices()
    pid = next((k for k, v in devs.items() if v[0] == ref and v[2] == path), None)
    if pid is None:
        return None
    return next(((s, i) for s, i in P.instances() if i.get("probe_id") == pid), None)


GRIDS = {"1/4": 1.0, "1/8": 0.5, "1/16": 0.25, "1/32": 0.125}


def _grid_offsets(onsets, grid):
    out_ = []
    for o in onsets:
        g = GRIDS[grid]
        off_beats = o["ppq"] - round(o["ppq"] / g) * g
        out_.append(off_beats * 60.0 / o["tempo"] * 1000.0)
    return out_


def _onsets_parallel(pairs, sec, threshold):
    import threading
    res, errs = {}, {}

    def job(s_, i):
        try:
            srv = P.Server(s_.path).connect()     # own connection: the two collections run at the same time
            res[i["uid"]] = srv.request({"cmd": "onsets", "uid": i["uid"], "seconds": sec, "threshold_db": threshold},
                                        timeout=sec + 15)
            srv.close()
        except (LiveError, OSError) as e:
            errs[i["uid"]] = str(e)

    th = [threading.Thread(target=job, args=p_) for p_ in pairs]
    for t_ in th:
        t_.start()
    for t_ in th:
        t_.join()
    if errs:
        raise LiveError("onsets: %s" % "; ".join(errs.values()))
    return res


def _timing(r, grid):
    offs = _grid_offsets(r["onsets"], grid)
    if not offs:
        return None
    med = statistics.median(offs)
    mad = statistics.median([abs(x - med) for x in offs])
    return {"onsets": len(offs), "median_ms": round(med, 3), "spread_ms": round(mad, 3)}


def _sync_ref_track():
    """Fallback reference: a temporary MIDI track with a short Drift click on every quarter note, from the session."""
    r = call("track_create", kind="midi", name="LiveCLI Sync Ref", index=-1)
    ref = r["ref"]
    call("insert_device", track=ref, name="Drift", index=0)
    ps = call("params", track=ref, device="0")["params"]
    for name, text in (("Env 1 Decay", "30 ms"), ("Env 1 Release", "20 ms"), ("Env 1 Sustain", "0 %")):
        p_ = next((x for x in ps if x["name"] == name), None)
        if p_:
            call("set_param", track=ref, device="0", param=p_["i"], text=text)
    call("clip_create", track=ref, slot=0, length=4.0)
    call("set_notes", track=ref, slot=0, notes=[{"pitch": 96, "start": float(k), "duration": 0.05, "velocity": 127} for k in range(4)])
    call("clip_fire", track=ref, slot=0)
    return ref


def _pulse_ref(sec):
    """Best reference: a temporary silent audio track where one probe emits impulses exactly on the quarter notes
    (Live Probe Protocol 'pulse') and a second probe right after it measures them. Returns (track ref, pulser, detector)."""
    r = call("track_create", kind="audio", name="LiveCLI Pulse Ref", index=-1)
    ref = r["ref"]
    call("set_routing", track=ref, side="input", type="No Input")
    call("set_mixer", track=ref, what="volume", value=0.0)        # -inf: the clicks never reach the speakers
    c_probe_add([ref, "pulse-src"])
    c_probe_add([ref, "pulse-det"])
    devs = track(ref)["devices"]
    src = _find_probe_at(ref, devs[0]["path"])
    det = _find_probe_at(ref, devs[1]["path"])
    if not src or not det or not src[0].has("pulse"):
        call("track_delete", track=ref)
        return None
    return ref, src, det


@command("sync", "sync <track> [reference-track] [seconds] [--grid 1/16] [--probes A B] [--keep]", "timing of a track "
         "against Live's grid (e.g. Overbridge or hardware on MIDI clock, playing their own sequencer): median offset of "
         "its transients from the grid, minus a reference track sequenced by Live (a temporary one if not given) = the "
         "Track Delay to set. Needs playback", "probes")
def c_sync(a):
    flags = [x for x in a if x.startswith("--")]
    grid = a[a.index("--grid") + 1] if "--grid" in a else "1/16"
    if grid not in GRIDS:
        raise LiveError("grid: one of %s" % ", ".join(GRIDS))
    args = [x for k, x in enumerate(a) if not x.startswith("--") and (k == 0 or a[k - 1] not in ("--grid", "--probes"))]
    nums = [x for x in args if re.fullmatch(r"\d+(\.\d+)?", x)]
    sec = float(nums[0]) if nums else 8.0
    args = [x for x in args if x not in nums]
    threshold = -50.0
    cleanup, temp_ref = [], None
    armed = [t_["ref"] for t_ in tracks() if t_.get("arm")] if "--probes" not in a else []
    if "--probes" in a:                       # two probes given directly: no Live lookups
        k = a.index("--probes")
        _, insts = _probe_pairs()
        pr, pt = P.find(insts, a[k + 2]), P.find(insts, a[k + 1])
        names = (P.describe(pt[1]), P.describe(pr[1]))
    else:
        need(args, 1, "sync <track> [reference-track] [seconds]")
        if not call("song")["playing"]:
            raise LiveError("start playback first (the devices must be playing in sync with Live)")
        t = track(args[0])
        pulse = None
        if len(args) > 1:
            rt = track(args[1])
        else:
            pulse = _pulse_ref(sec)
            if pulse:
                temp_ref, src, pr = pulse
                rt = track(temp_ref)
            else:
                temp_ref = _sync_ref_track()
                rt = track(temp_ref)
                time.sleep(1.0)

        def probe_at_end(tr):
            devs = track(tr["ref"])["devices"]
            if devs:
                pr_ = _find_probe_at(tr["ref"], devs[-1]["path"])
                if pr_:
                    return pr_
            c_probe_add([tr["ref"], "sync"])
            cleanup.append(tr["ref"])
            return _find_probe_at(tr["ref"], track(tr["ref"])["devices"][-1]["path"])

        pt = probe_at_end(t)
        if not pulse:
            pr = probe_at_end(rt)
        if not pt or not pr:
            raise LiveError("could not place the probes at the end of the two tracks")
        names = (t["name"], rt["name"] + (" (temporary: probe impulses on quarters)" if pulse else
                                          " (temporary, Drift click on quarters)" if temp_ref else ""))
        if pulse:
            r_ = src[0].request({"cmd": "pulse", "uid": src[1]["uid"], "every": 1.0, "offset": 0.0,
                                 "seconds": min(10.0, sec + 2.0), "level_db": -20, "mode": "replace"})
            time.sleep(0.3)
    try:
        if not pt[0].has("onsets") or not pr[0].has("onsets"):
            raise LiveError("the probes don't support 'onsets' (needs Reference/Loudline Probe 0.1.2+)")
        res = _onsets_parallel([pt, pr], sec, threshold)
        mt, mr = _timing(res[pt[1]["uid"]], grid), _timing(res[pr[1]["uid"]], grid)
    finally:
        if "--keep" not in flags:
            for ref_ in cleanup:
                cur = track(ref_)["devices"]
                for k in range(len(cur) - 1, -1, -1):
                    x = _find_probe_at(ref_, cur[k]["path"])
                    if x and x[1].get("label") == "sync":
                        call("delete_device", track=ref_, device=cur[k]["path"])
            if temp_ref:
                call("track_delete", track=temp_ref)
            for ref_ in armed:                      # creating a MIDI track can steal the arm (exclusive arm)
                try:
                    call("track_set", track=ref_, arm=True)
                except LiveError:
                    pass
    if not mt or not mr:
        raise LiveError("no transients detected on %s" % (names[0] if not mt else names[1]))
    late = round(mt["median_ms"] - mr["median_ms"], 2)
    resd = {"track": names[0], "reference": names[1], "grid": grid, "seconds": sec, "track_timing": mt,
            "reference_timing": mr, "late_ms": late, "track_delay_ms": -late}
    if JSON:
        return out(resd)
    print("%s vs %s, %gs, grid %s" % (names[0], names[1], sec, grid))
    print("  track:     %d transients, median %+.2f ms from the grid (spread %.2f ms)" % (mt["onsets"], mt["median_ms"], mt["spread_ms"]))
    print("  reference: %d transients, median %+.2f ms from the grid (spread %.2f ms)" % (mr["onsets"], mr["median_ms"], mr["spread_ms"]))
    if abs(late) < 0.1:
        print("  in time: no Track Delay needed")
    else:
        print("  %s is %s by %.2f ms: set its Track Delay to %+.2f ms" % (names[0], "late" if late > 0 else "early", abs(late), -late))
    if mt["spread_ms"] > 1.5:
        print("  warning: the track's transients are spread (swing, humanized or off-grid notes): use a straight part, "
              "like a kick on quarters, for a precise value")


def file_onsets(path, offset_s, duration_s, sr=48000, refractory_s=0.05):
    """Transient times (s, from offset) in an audio file, standard library + ffmpeg. An onset is the first sample whose
    1 ms envelope reaches half (-6 dB) of the transient's peak; refractory 50 ms."""
    import array
    import subprocess
    raw = subprocess.run(["ffmpeg", "-v", "error", "-ss", "%.6f" % offset_s, "-t", "%.6f" % duration_s, "-i", path,
                          "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"], capture_output=True).stdout
    x = array.array("f")
    x.frombytes(raw[: len(raw) // 4 * 4])
    n = len(x)
    if n == 0:
        raise LiveError("could not decode %s (is ffmpeg installed?)" % path)
    # high-pass ~150 Hz first: the attack of a kick or click is rich in highs, the ripple of low notes is not
    import math as _m
    rc = 1.0 / (2 * _m.pi * 150.0)
    alpha = rc / (rc + 1.0 / sr)
    hp, y, xp = [0.0] * n, 0.0, 0.0
    for k in range(n):
        y = alpha * (y + x[k] - xp)
        xp = x[k]
        hp[k] = abs(y)
    w = max(1, sr // 500)                    # 2 ms envelope
    env = [0.0] * n
    acc = 0.0
    for k in range(n):
        acc += hp[k]
        if k >= w:
            acc -= hp[k - w]
        env[k] = acc / w
    top = max(env)
    if top <= 0:
        return []
    thr = top * 0.2
    refr, look = int(refractory_s * sr), int(0.01 * sr)
    out_, k = [], 0
    while k < n:
        if env[k] >= thr and (k == 0 or env[k - 1] < thr):
            peak = max(env[k: k + look])
            j = k
            while j > 0 and env[j - 1] >= 0.5 * peak and k - j < look:
                j -= 1
            while env[j] < 0.5 * peak:
                j += 1
            out_.append(j / float(sr))
            k += refr
        else:
            k += 1
    return out_


@command("grid", "grid <track> [bars] [start-bar] [--grid 1/16] [--keep]", "record a track (bounce) and measure where its "
         "transients fall against Live's grid: median offset, spread. Works with any probe or none", "audio")
def c_grid(a):
    need(a, 1, "grid <track> [bars] [start-bar]")
    grid = a[a.index("--grid") + 1] if "--grid" in a else "1/16"
    pos = [x for k, x in enumerate(a[1:], 1) if not x.startswith("--") and a[k - 1] != "--grid"]
    bars = float(pos[0]) if pos else 4.0
    start_bar = float(pos[1]) if len(pos) > 1 else 9.0
    t = track(a[0])
    s_ = call("song")
    global JSON
    js, JSON = JSON, True                    # run bounce quietly and capture its result
    buf = __import__("io").StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        c_bounce([t["ref"], str(start_bar), str(bars), "LiveCLI grid %s" % t["name"]])
    JSON = js
    b = json.loads(buf.getvalue())
    try:
        if not b.get("file") or "file_offset_s" not in b:
            raise LiveError("bounce did not produce a single clip")
        # at most one transient per grid step: anything closer than 60% of a step is the same note
        times = file_onsets(b["file"], b["file_offset_s"], b["duration_s"],
                            refractory_s=0.6 * GRIDS[grid] * 60.0 / s_["tempo"])
    finally:
        if "--keep" not in a:
            call("track_delete", track=b["track"])
    g = GRIDS[grid]
    beat_s = 60.0 / s_["tempo"]
    offs = []
    for tt in times:
        beats = tt / beat_s
        offs.append((beats - round(beats / g) * g) * beat_s * 1000.0)
    if not offs:
        raise LiveError("no transients found in the recording")
    med = statistics.median(offs)
    mad = statistics.median([abs(x - med) for x in offs])
    res = {"track": t["name"], "bars": bars, "start_bar": start_bar, "grid": grid, "onsets": len(offs),
           "median_ms": round(med, 3), "spread_ms": round(mad, 3), "file": b["file"] if "--keep" in a else None}
    out(res, "%s: %d transients in %g bars, median %+.2f ms from the %s grid (spread %.2f ms)" % (
        t["name"], len(offs), bars, med, grid, mad))


@command("latency", "latency <track> <device> [seconds] [--keep]", "real latency of a device or hardware insert, "
         "measured with two probes around it, vs the latency Live assumes (needs playback with signal)", "probes")
def c_latency(a):
    need(a, 2, "latency <track> <device> [seconds] [--keep]")
    t = track(a[0])
    d = find_device(t, a[1])
    if "." in d["path"]:
        raise LiveError("latency works on top-level devices")
    nums = [x for x in a[2:] if not x.startswith("--")]
    sec = float(nums[0]) if nums else 5.0
    if not call("song")["playing"]:
        raise LiveError("start playback first: the measurement needs audio through %s" % d["name"])
    added = []
    i = int(d["path"])
    # probe before the device
    pre = _find_probe_at(t["ref"], str(i - 1)) if i > 0 else None
    if pre is None:
        if i == 0:
            c_probe_add([t["ref"], "first", "lat-pre"])
        else:
            c_probe_add([t["ref"], "after", str(i - 1), "lat-pre"])
        added.append("lat-pre")
        i += 1
    post = _find_probe_at(t["ref"], str(i + 1))
    if post is None:
        c_probe_add([t["ref"], "after", str(i), "lat-post"])
        added.append("lat-post")
    pre = _find_probe_at(t["ref"], str(i - 1))
    post = _find_probe_at(t["ref"], str(i + 1))
    if not pre or not post:
        raise LiveError("could not find the two probes around %s" % d["name"])
    if pre[0].path != post[0].path or not pre[0].has("delay"):
        raise LiveError("the probes must be the same product and support 'delay' (Reference/Loudline Probe 0.1.1+)")
    max_lag = 1000
    r = pre[0].request({"cmd": "delay", "a": pre[1]["uid"], "b": post[1]["uid"], "seconds": sec, "max_lag_ms": max_lag},
                       timeout=sec + 2 * max_lag / 1000.0 + 5)
    dev = next(x for x in track(t["ref"])["devices"] if x["path"] == str(i))
    live_smp = dev.get("latency_samples")
    res = {"track": t["name"], "device": dev["name"], "class": dev["class"], "measured_samples": r["delay_samples"],
           "measured_ms": r["delay_ms"], "live_assumes_samples": live_smp, "live_assumes_ms": dev.get("latency_ms"),
           "correlation_peak": r.get("correlation_peak"), "polarity": r.get("polarity"),
           "ambiguity": r.get("ambiguity"), "aligned": r.get("aligned"), "sample_rate": r.get("sample_rate")}
    if live_smp is not None:
        res["difference_samples"] = r["delay_samples"] - live_smp
        res["difference_ms"] = round(res["difference_samples"] / r["sample_rate"] * 1000, 3)
    if "--keep" not in a:
        cur = track(t["ref"])["devices"]
        for k in range(len(cur) - 1, -1, -1):
            pr = _find_probe_at(t["ref"], cur[k]["path"])
            if pr and pr[1].get("label") in added:
                call("delete_device", track=t["ref"], device=cur[k]["path"])
    if JSON:
        return out(res)
    warn = []
    if (r.get("correlation_peak") or 0) < 0.5:
        warn.append("low correlation: the device changes the signal a lot, the result may be wrong")
    if (r.get("ambiguity") or 0) > 0.5:
        warn.append("ambiguous: periodic signal (a loop or a steady tone), use noise or a full mix")
    if r.get("polarity") == -1:
        warn.append("polarity is inverted between before and after")
    print("%s / %s: measured %d samples (%.2f ms), Live assumes %s" % (
        t["name"], dev["name"], r["delay_samples"], r["delay_ms"],
        "%d samples (%.2f ms)" % (live_smp, dev.get("latency_ms") or 0) if live_smp is not None else "?"))
    if live_smp is not None:
        dms = res["difference_ms"]
        if abs(res["difference_samples"]) <= 2:
            print("  matches: Live compensates this device correctly")
        else:
            print("  difference %+d samples (%+.2f ms): %s" % (res["difference_samples"], dms,
                  "add %.2f ms to the device's Hardware Latency" % dms if dev["class"] == "ProxyAudioEffectDevice" and dms > 0 else
                  "remove %.2f ms from the device's Hardware Latency" % -dms if dev["class"] == "ProxyAudioEffectDevice" else
                  "the signal after it is late by this much and Live does not compensate it (fine for a device that "
                  "delays on purpose, like Delay or Align Delay; for a plugin it means unreported latency)"))
    print("  correlation %.2f, ambiguity %.2f, alignment %s" % (r.get("correlation_peak") or 0, r.get("ambiguity") or 0, r.get("aligned")))
    for w in warn:
        print("  warning: " + w)


# ================================================================== clips: properties, editing, automation
def _kv(items):
    out_ = {}
    for it in items:
        if "=" not in it:
            raise LiveError("expected key=value, got '%s'" % it)
        k, v = it.split("=", 1)
        out_[k.replace("-", "_")] = v
    return out_


def _bool(v):
    return str(v).lower() in ("on", "true", "yes", "1")


@command("clip-info", "clip-info <track> <slot|@arr>", "all properties of a clip (launch, markers, warp, gain...)", "clips")
def c_clip_info(a):
    need(a, 2, "clip-info <track> <slot|@arr>")
    r = call("clip_set", track=track(a[0])["ref"], **clip_ref(a[1]))
    out(r, "\n".join("  %-20s %s" % (k, v) for k, v in r.items() if v is not None))


@command("clip-set", "clip-set <track> <slot|@arr> key=value...", "keys: name color looping muted legato loop_start loop_end "
         "start_marker end_marker (beats) launch_mode=trigger|gate|toggle|repeat launch_quantization=global|none|1 bar|1/4... "
         "velocity_amount warping warp_mode=beats|tones|texture|re-pitch|complex|complex pro gain=-3dB pitch_coarse pitch_fine "
         "ram_mode", "clips")
def c_clip_set(a):
    need(a, 3, "clip-set <track> <slot|@arr> key=value...")
    kw = _kv(a[2:])
    for k in ("looping", "muted", "legato", "warping", "ram_mode"):
        if k in kw:
            kw[k] = _bool(kw[k])
    for k in ("loop_start", "loop_end", "start_marker", "end_marker", "velocity_amount", "pitch_fine"):
        if k in kw:
            kw[k] = float(kw[k])
    if "pitch_coarse" in kw:
        kw["pitch_coarse"] = int(kw["pitch_coarse"])
    r = call("clip_set", track=track(a[0])["ref"], **dict(kw, **clip_ref(a[1])))
    out(r, "\n".join("  %-20s %s" % (k, v) for k, v in r.items() if v is not None))


@command("clip-set-all", "clip-set-all <tracks|all> [--session|--arrangement] [--audio|--midi] [--name text] key=value...",
         "change many clips at once (one undo step), e.g. after importing files: clip-set-all all --audio warping=off. "
         "tracks: comma-separated (t3,t4 or names) or all", "clips")
def c_clip_set_all(a):
    need(a, 2, "clip-set-all <tracks|all> [filters] key=value...")
    flags = {x for x in a if x.startswith("--")}
    name_f = a[a.index("--name") + 1].lower() if "--name" in a else None
    kv = [x for k, x in enumerate(a[1:], 1) if "=" in x and a[k - 1] != "--name"]
    if not kv:
        raise LiveError("nothing to set: give key=value (see 'live help clip-set')")
    ts = tracks()
    refs = [t["ref"] for t in ts if t["ref"][0] == "t"] if a[0] == "all" else [find_track(ts, q)["ref"] for q in a[0].split(",")]
    where = ("session",) if "--session" in flags else ("arrangement",) if "--arrangement" in flags else ("session", "arrangement")
    targets = []
    for ref in refs:
        if "session" in where:
            targets += [(ref, {"slot": c["slot"]}, c) for c in call("clips", track=ref)]
        if "arrangement" in where:
            targets += [(ref, {"arr": c["arr"]}, c) for c in call("arrangement", track=ref)]
    if "--audio" in flags:
        targets = [x for x in targets if not x[2]["midi"]]
    if "--midi" in flags:
        targets = [x for x in targets if x[2]["midi"]]
    if name_f:
        targets = [x for x in targets if name_f in (x[2]["name"] or "").lower()]
    if not targets:
        return out({"changed": 0}, "no clips match")
    call("undo_begin")
    done, errors = 0, []
    try:
        for ref, cr, c in targets:
            try:
                kw = _kv(kv)
                for k in ("looping", "muted", "legato", "warping", "ram_mode"):
                    if k in kw:
                        kw[k] = _bool(kw[k])
                for k in ("loop_start", "loop_end", "start_marker", "end_marker", "velocity_amount", "pitch_fine"):
                    if k in kw:
                        kw[k] = float(kw[k])
                if "pitch_coarse" in kw:
                    kw["pitch_coarse"] = int(kw["pitch_coarse"])
                call("clip_set", track=ref, **dict(kw, **cr))
                done += 1
            except LiveError as e:
                errors.append("%s %s: %s" % (ref, c.get("name") or cr, e))
    finally:
        call("undo_end")
    out({"changed": done, "errors": errors}, "changed %d clips%s" % (done, "".join("\n  error " + e for e in errors)))


@command("clip-edit", "clip-edit <track> <slot|@arr> quantize [1/16] [amount%] | transpose <semitones> | velocity <percent> "
         "| crop | duplicate-loop", "edit a clip's notes or region (one undo step in Live)", "clips")
def c_clip_edit(a):
    need(a, 3, "clip-edit <track> <slot|@arr> <op> ...")
    ref, cr, op = track(a[0])["ref"], clip_ref(a[1]), a[2]
    kw = {"op": op.replace("-", "_")}
    if op == "quantize":
        kw["grid"] = a[3] if len(a) > 3 else "1/16"
        kw["amount"] = float(a[4].rstrip("%")) / 100.0 if len(a) > 4 else 1.0
    elif op == "transpose":
        need(a, 4, "clip-edit <track> <clip> transpose <semitones>")
        kw["semitones"] = int(a[3])
    elif op == "velocity":
        need(a, 4, "clip-edit <track> <clip> velocity <percent>")
        kw["percent"] = float(a[3].rstrip("%"))
    out(call("clip_edit", track=ref, **dict(kw, **cr)), "%s done" % op)


@command("warp-markers", "warp-markers <track> <slot|@arr>", "warp markers of an audio clip (beat and sample time)", "clips")
def c_warp_markers(a):
    need(a, 2, "warp-markers <track> <slot|@arr>")
    r = call("warp_markers", track=track(a[0])["ref"], **clip_ref(a[1]))
    out(r, "\n".join("  beat %8.3f  sample time %9.4f s" % (m["beat"], m["sample_time"]) for m in r))


def _target(t, dev_q, param_q):
    """A parameter target: <device> <param>, or 'mixer' volume|pan|send <A>|activator, or 'chain' <D.C> volume|pan."""
    if dev_q == "mixer":
        q = param_q.split()
        if q[0] == "send":
            return {"mixer": "send", "index": ord(q[1].lower()) - 97 if q[1].isalpha() else int(q[1])}
        return {"mixer": q[0]}
    if dev_q.startswith("chain:"):
        return {"chain": dev_q[6:], "chain_mixer": param_q}
    d = find_device(t, dev_q)
    ps = call("params", track=t["ref"], device=d["path"])["params"]
    return {"device": d["path"], "param": find_param(ps, param_q)["i"]}


@command("envelope", "envelope <track> <slot|@arr> <device|mixer> <param> [points]", "read a clip's automation envelope "
         "for a parameter ('mixer volume', 'mixer \"send A\"')", "clips")
def c_envelope(a):
    need(a, 4, "envelope <track> <clip> <device|mixer> <param> [points]")
    t = track(a[0])
    kw = dict(_target(t, a[2], a[3]), **clip_ref(a[1]))
    r = call("envelope", track=t["ref"], points=int(a[4]) if len(a) > 4 else 17, **kw)
    if JSON or not r["exists"]:
        return out(r, "no envelope for %s in this clip" % r["param"])
    print("%s envelope:" % r["param"])
    for p_ in r["points"]:
        print("  beat %7.2f  %s" % (p_["time"], p_["text"]))


@command("envelope-set", "envelope-set <track> <slot|@arr> <device|mixer> <param> <start:length=value>... [--clear]",
         "write automation steps in a clip, e.g. 0:4=500Hz 4:4=2kHz 8:8=-6dB (beats)", "clips")
def c_envelope_set(a):
    need(a, 5, "envelope-set <track> <clip> <device|mixer> <param> <start:length=value>...")
    t = track(a[0])
    steps = []
    for it in a[4:]:
        if it == "--clear":
            continue
        tl, v = it.split("=", 1)
        st, ln = tl.split(":")
        step = {"time": float(st), "duration": float(ln)}
        if v.startswith("#"):
            step["value"] = float(v[1:])
        else:
            step["text"] = v
        steps.append(step)
    kw = dict(_target(t, a[2], a[3]), **clip_ref(a[1]))
    out(call("envelope_set", track=t["ref"], steps=steps, clear="--clear" in a, **kw))


@command("envelope-clear", "envelope-clear <track> <slot|@arr> [<device|mixer> <param>|--all]", "remove automation from a clip", "clips")
def c_envelope_clear(a):
    need(a, 3, "envelope-clear <track> <clip> <device|mixer> <param> | --all")
    t = track(a[0])
    if a[2] == "--all":
        return out(call("envelope_clear", track=t["ref"], all=True, **clip_ref(a[1])))
    need(a, 4, "envelope-clear <track> <clip> <device|mixer> <param>")
    out(call("envelope_clear", track=t["ref"], **dict(_target(t, a[2], a[3]), **clip_ref(a[1]))))


@command("automation", "automation", "parameters with automation playing or overridden (switched off by a manual change)", "info")
def c_automation(a):
    r = call("automation", timeout=20)
    out(r, "\n".join("  %-4s %-22s %-40s %-10s %s" % (x["track"], x["track_name"][:22], x["param"][:40], x["state"], x["text"])
                     for x in r) or "no automated parameters")


@command("re-enable-automation", "re-enable-automation", "Live's Re-Enable Automation (all overridden parameters)", "transport")
def c_re_enable(a):
    out(call("re_enable_automation"), "automation re-enabled")


@command("arr-add", "arr-add <track> <bar> slot <N> | file <path> | midi <bars>", "put a clip in the arrangement: copy of "
         "a session clip, an audio file, or an empty MIDI clip", "clips")
def c_arr_add(a):
    need(a, 3, "arr-add <track> <bar> slot <N> | file <path> | midi <bars>")
    t = track(a[0])
    s_ = call("song")
    at = bar_to_beats(a[1], s_)
    kw = {"track": t["ref"], "time": at}
    if a[2] == "slot":
        kw["slot"] = int(a[3])
    elif a[2] == "file":
        kw["file"] = os.path.abspath(" ".join(a[3:]))
    elif a[2] == "midi":
        n, d = [int(x) for x in s_["signature"].split("/")]
        kw["length"] = float(a[3] if len(a) > 3 else 1) * n * 4.0 / d
    else:
        raise LiveError("arr-add <track> <bar> slot <N> | file <path> | midi <bars>")
    r = call("arr_add", **kw)
    out(r, "\n".join("  @%d  bar %7.2f - %7.2f  %s" % (c["arr"], c["start_bar"], c["end_bar"], c["name"]) for c in r))


@command("preset", "preset <track> <device> [index]", "a plugin's own presets (when it publishes them to Live) and the "
         "names of all its parameters, also those not configured in Live; with index, select a preset", "devices")
def c_preset(a):
    need(a, 2, "preset <track> <device> [index]")
    t = track(a[0])
    d = find_device(t, a[1])
    r = call("presets", track=t["ref"], device=d["path"], index=int(a[2]) if len(a) > 2 else None)
    if JSON:
        return out(r)
    print("%s / %s: %d presets%s, %d of %d parameters exposed to Live" % (t["name"], r["device"], len(r["presets"]),
          ", selected %s" % r["selected"] if r["presets"] else "", r["exposed"], len(r["all_parameter_names"]) or r["exposed"]))
    for i, p_ in enumerate(r["presets"][:60]):
        print("  %s%3d %s" % ("*" if i == r["selected"] else " ", i, p_))
    exposed = {p_["name"] for p_ in call("params", track=t["ref"], device=d["path"])["params"]}
    hidden = [n for n in r["all_parameter_names"] if n not in exposed]
    if hidden:
        print("  %d not exposed (Configure in Live to use them): %s%s" % (len(hidden), ", ".join(hidden[:25]),
              " ..." if len(hidden) > 25 else ""))


@command("sidechain", "sidechain <track> <device> [source-type] [channel]", "show or set a device's sidechain input "
         "(Compressor, Gate, Auto Filter...)", "devices")
def c_sidechain(a):
    need(a, 2, "sidechain <track> <device> [source-type] [channel]")
    t = track(a[0])
    d = find_device(t, a[1])
    r = call("device_routing", track=t["ref"], device=d["path"], type=a[2] if len(a) > 2 else None,
             channel=a[3] if len(a) > 3 else None)
    out(r, "%s / %s: sidechain from %s / %s\n  sources: %s\n  channels: %s" % (t["name"], r["device"], r["type"], r["channel"],
        " | ".join(r["types"]), " | ".join(r["channels"])))


@command("warp-move", "warp-move <track> <slot|@arr> <beat> <distance>", "move a warp marker by a distance in beats "
         "(Live's API can move warp markers, not add or remove them)", "clips")
def c_warp_move(a):
    need(a, 4, "warp-move <track> <clip> <beat> <distance>")
    out(call("warp_marker_move", track=track(a[0])["ref"], beat=float(a[2]), distance=float(a[3]), **clip_ref(a[1])))


@command("groove", "groove [<track> <slot|@arr> [index|clear]] [--amount 0..1.3]", "the set's groove pool; apply a "
         "groove to a clip or clear it; global groove amount", "clips")
def c_groove(a):
    kw = {}
    if "--amount" in a:
        kw["amount"] = float(a[a.index("--amount") + 1])
    pos = [x for k, x in enumerate(a) if not x.startswith("--") and (k == 0 or a[k - 1] != "--amount")]
    if len(pos) >= 2:
        kw.update(track=track(pos[0])["ref"], **clip_ref(pos[1]))
        if len(pos) > 2:
            if pos[2] == "clear":
                kw["clear"] = True
            else:
                kw["index"] = int(pos[2])
    r = call("groove", **kw)
    out(r, "\n".join(["groove pool:"] + ["  %d %s" % (g["i"], g["name"]) for g in r["pool"]] +
                     (["clip groove: %s" % r.get("clip_groove")] if "clip_groove" in r else []) + ["amount: %s" % r.get("amount")]))


@command("arr-from-session", "arr-from-session <scene:bars>... [--at bar]", "lay session scenes out in the arrangement, "
         "e.g. 0:8 1:16 2:8 (scene 0 for 8 bars, then scene 1 for 16...)", "clips")
def c_arr_from_session(a):
    need(a, 1, "arr-from-session <scene:bars>... [--at bar]")
    at = bar_to_beats(a[a.index("--at") + 1]) if "--at" in a else 0.0
    sc = [[int(x.split(":")[0]), float(x.split(":")[1])] for k, x in enumerate(a) if ":" in x and (k == 0 or a[k - 1] != "--at")]
    r = call("arr_from_session", scenes=sc, time=at)
    out(r, "%d clips placed, ending at bar %g" % (r["clips"], r["end_bar"]))


# ================================================================== racks and native devices
@command("rack", "rack <track> <device>", "chains of a rack (mixer, devices), macros, variations, Drum Rack pads", "devices")
def c_rack(a):
    need(a, 2, "rack <track> <device>")
    t = track(a[0])
    d = find_device(t, a[1])
    r = call("rack", track=t["ref"], device=d["path"])
    if JSON:
        return out(r)
    print("%s / %s (%s): %d variations%s" % (t["name"], r["device"], r["class"], r["variations"],
          ", selected %d" % r["selected_variation"] if r["selected_variation"] >= 0 else ""))
    for c in r["chains"]:
        print("  chain %s.%d %-20s %s %s vol %s pan %s: %s" % (d["path"], c["i"], c["name"][:20], "MUTE" if c["mute"] else "    ",
              "SOLO" if c["solo"] else "    ", c["volume"], c["pan"], ", ".join(c["devices"])))
    print("  macros: " + ", ".join("%s=%s" % (m["name"], m["text"]) for m in r["macros"]))
    for p_ in r.get("pads", []):
        print("  pad %3d %-16s %s%s" % (p_["note"], p_["name"], "MUTE " if p_["mute"] else "", "SOLO" if p_["solo"] else ""))


@command("chain", "chain <track> <D.C> [name=..] [mute=on|off] [solo=on|off] [volume=-6dB] [pan=20L]", "rack chain settings "
         "(D.C = rack device path + chain index, e.g. 2.0)", "devices")
def c_chain(a):
    need(a, 2, "chain <track> <D.C> key=value...")
    t = track(a[0])
    kw = _kv(a[2:])
    res = {}
    for k in ("volume", "pan"):
        if k in kw:
            v = kw.pop(k)
            if k == "volume" and parse_number(v) is not None and not v.lower().endswith("db"):
                v += " dB"
            res[k] = call("param_target", track=t["ref"], chain=a[1], chain_mixer=k, text=v)["change"]["after"]
    for k in ("mute", "solo"):
        if k in kw:
            kw[k] = _bool(kw[k])
    if kw:
        res.update(call("chain_set", track=t["ref"], chain=a[1], **kw))
    out(res)


@command("chain-new", "chain-new <track> <rack device> [name]", "add a chain to a rack", "devices")
def c_chain_new(a):
    need(a, 2, "chain-new <track> <rack device> [name]")
    t = track(a[0])
    out(call("chain_insert", track=t["ref"], device=find_device(t, a[1])["path"], name=" ".join(a[2:]) or None))


@command("variation", "variation <track> <rack> store|recall <i>|delete <i>|randomize", "rack macro variations", "devices")
def c_variation(a):
    need(a, 3, "variation <track> <rack> store|recall <i>|delete <i>|randomize")
    t = track(a[0])
    kw = {"op": a[2]}
    if a[2] in ("recall", "delete"):
        need(a, 4, "variation <track> <rack> %s <i>" % a[2])
        kw["index"] = int(a[3])
    out(call("variation", track=t["ref"], device=find_device(t, a[1])["path"], **kw))


@command("pad", "pad <track> <drum rack> <note> mute|solo on|off", "mute or solo a Drum Rack pad", "devices")
def c_pad(a):
    need(a, 5, "pad <track> <drum rack> <note> mute|solo on|off")
    t = track(a[0])
    out(call("pad_set", track=t["ref"], device=find_device(t, a[1])["path"], note=int(a[2]), **{a[3]: _bool(a[4])}))


@command("insert", "insert <track> <Live device name> [index] [chain D.C]", "insert a Live device directly (no browser): "
         "'EQ Eight', 'Compressor', 'Utility'...", "devices")
def c_insert(a):
    need(a, 2, "insert <track> <device name> [index] [chain D.C]")
    t = track(a[0])
    rest = a[1:]
    chain = None
    if "chain" in rest:
        k = rest.index("chain")
        chain = rest[k + 1]
        rest = rest[:k] + rest[k + 2:]
    idx = None
    if len(rest) > 1 and rest[-1].isdigit():
        idx = int(rest[-1])
        rest = rest[:-1]
    kw = {"track": t["ref"], "name": " ".join(rest), "chain": chain}
    if idx is not None:
        kw["index"] = idx
    r = call("insert_device", **kw)
    out(r, "%s: %s" % (t["name"], ", ".join(r["devices"])))


@command("duplicate", "duplicate <track> <device>", "duplicate a device (placed right after it)", "devices")
def c_duplicate(a):
    need(a, 2, "duplicate <track> <device>")
    t = track(a[0])
    r = call("duplicate_device", track=t["ref"], device=find_device(t, a[1])["path"])
    out(r, "%s: %s" % (t["name"], ", ".join(r["devices"])))


# ================================================================== mixer extras and view
@command("pan-mode", "pan-mode <track> stereo|split [left right]", "Live's split stereo panning, e.g. pan-mode t3 split 50L 50R", "tracks")
def c_pan_mode(a):
    need(a, 2, "pan-mode <track> stereo|split [left right]")
    t = track(a[0])
    r = call("mixer_set", track=t["ref"], panning_mode=a[1])
    if a[1] == "split" and len(a) >= 4:
        call("param_target", track=t["ref"], mixer="split_left", text=a[2])
        call("param_target", track=t["ref"], mixer="split_right", text=a[3])
    out(r)


@command("crossfade", "crossfade <track> A|none|B", "crossfader assignment", "tracks")
def c_crossfade(a):
    need(a, 2, "crossfade <track> A|none|B")
    out(call("mixer_set", track=track(a[0])["ref"], crossfade=a[1]))


@command("activator", "activator <track> on|off", "track activator (the track on/off button: no processing when off)", "tracks")
def c_activator(a):
    need(a, 2, "activator <track> on|off")
    r = call("param_target", track=track(a[0])["ref"], mixer="activator", value=1.0 if _bool(a[1]) else 0.0)
    out(r, "%s: activator %s" % (a[0], r["change"]["after"]))


@command("cue", "cue [dB]", "preview/cue volume (master)", "tracks")
def c_cue(a):
    kw = {"text": a[0] + ("" if a[0].lower().endswith("db") else " dB")} if a else {}
    r = call("param_target", track="m", mixer="cue", **kw)
    out(r, "cue volume %s" % (r["change"]["after"] if a else r["text"]))


@command("show", "show <track> [device|@clip|slot]", "bring a track, a device or a clip into view (to show the user)", "other")
def c_show(a):
    need(a, 1, "show <track> [device|slot]")
    t = track(a[0])
    kw = {"track": t["ref"]}
    if len(a) > 1:
        if a[1].isdigit() and int(a[1]) < 1000 and not any(d["path"] == a[1] for d in t["devices"]):
            kw["slot"] = int(a[1])
        else:
            kw["device"] = find_device(t, a[1])["path"]
    out(call("show", **kw), "shown")


@command("view", "view Session|Arranger|Detail/Clip|Detail/DeviceChain|Browser", "show and focus one of Live's views", "other")
def c_view(a):
    need(a, 1, "view <name>")
    out(call("show", view=a[0]), "ok")


# ================================================================== snapshot, diff, restore, events
SNAP_DIR = os.path.join(CONFIG_DIR, "snapshots")


def _snap_path(name):
    return os.path.join(SNAP_DIR, name if name.endswith(".json") else name + ".json")


def _take():
    return {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "values": call("snapshot", timeout=30)}


def _stable_keys(values):
    """Map snapshot keys to keys that survive device moves: a device is 'track | name #n' (n-th device with that name
    on the track, in chain order), not its position. Returns {stable: (key, label, raw, text)}."""
    out_, seen = {}, {}
    for k, lab, v, txt in values:
        parts = k.split("|")
        if len(parts) >= 5 and parts[1] == "dev":
            dev_id = (parts[0], parts[2], parts[4])
            if dev_id not in seen:
                n = sum(1 for d in seen if d[0] == parts[0] and d[2] == parts[4])
                seen[dev_id] = n
            sk = "%s|dev|%s#%d|%s" % (parts[0], parts[4], seen[dev_id], parts[3])
        else:
            sk = k
        out_[sk] = (k, lab, v, txt)
    return out_


def _diff(old, new):
    """changed: (current key, label, old text, new text, old raw, new raw); added/removed: (key, label, text)."""
    o, n = _stable_keys(old["values"]), _stable_keys(new["values"])
    changed = [(n[k][0], n[k][1], o[k][3], n[k][3], o[k][2], n[k][2]) for k in n if k in o and o[k][2] != n[k][2]]
    added = [(n[k][0], n[k][1], n[k][3]) for k in n if k not in o]
    removed = [(o[k][0], o[k][1], o[k][3]) for k in o if k not in n]
    return changed, added, removed


@command("snapshot", "snapshot [name] [--measure [seconds]]", "save every mixer, device and chain value of the set "
         "(default name: time); --measure also stores what every probe measures, for 'live explain'", "other")
def c_snapshot(a):
    os.makedirs(SNAP_DIR, exist_ok=True)
    pos = [x for k, x in enumerate(a) if not x.startswith("--") and (k == 0 or a[k - 1] != "--measure")]
    name = pos[0] if pos else time.strftime("%Y%m%d-%H%M%S")
    snap = _take()
    if "--measure" in a:
        k = a.index("--measure")
        sec = float(a[k + 1]) if len(a) > k + 1 and a[k + 1].replace(".", "").isdigit() else 5.0
        from . import cmd_mix
        snap["measure"] = cmd_mix.measure_all_probes(sec)
    with open(_snap_path(name), "w") as fh:
        json.dump(snap, fh)
    out({"name": name, "values": len(snap["values"])}, "snapshot '%s': %d values" % (name, len(snap["values"])))


@command("snapshots", "snapshots", "saved snapshots", "other")
def c_snapshots(a):
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "*.json")), key=os.path.getmtime)
    rows = [{"name": os.path.basename(f)[:-5], "time": json.load(open(f)).get("time")} for f in files]
    out(rows, "\n".join("  %-24s %s" % (r["name"], r["time"]) for r in rows) or "no snapshots")


@command("diff", "diff [old-snapshot] [new-snapshot]", "what changed: between two snapshots, or from one (default: the "
         "latest) to now", "other")
def c_diff(a):
    files = sorted(glob.glob(os.path.join(SNAP_DIR, "*.json")), key=os.path.getmtime)
    if not a and not files:
        raise LiveError("no snapshots yet: 'live snapshot' first")
    old = json.load(open(_snap_path(a[0]) if a else files[-1]))
    new = json.load(open(_snap_path(a[1]))) if len(a) > 1 else _take()
    changed, added, removed = _diff(old, new)
    if JSON:
        return out({"changed": changed, "added": added, "removed": removed})
    print("since %s: %d changed, %d new, %d gone" % (old["time"], len(changed), len(added), len(removed)))
    for k, lab, ot, nt, _, _ in changed:
        print("  %-60s %s -> %s" % (lab[:60], ot, nt))
    for k, lab, txt in added[:40]:
        print("  + %-58s %s" % (lab[:58], txt))
    for k, lab, txt in removed[:40]:
        print("  - %-58s %s" % (lab[:58], txt))


@command("restore", "restore <snapshot> [filter]", "put back the values of a snapshot that differ now (only those whose "
         "label contains filter); one undo step", "other")
def c_restore(a):
    need(a, 1, "restore <snapshot> [filter]")
    old = json.load(open(_snap_path(a[0])))
    f = " ".join(a[1:]).lower()
    changed, _, _ = _diff(old, _take())
    items = [[k, ov] for k, lab, _, _, ov, _ in changed if not f or f in lab.lower()]
    if not items:
        return out({"set": 0, "skipped": []}, "nothing to restore")
    call("undo_begin")
    try:
        r = call("set_values", items=items, timeout=30)
    finally:
        call("undo_end")
    if JSON:
        return out(r)
    print("restored %d values" % r["set"])
    for k, why in r["skipped"]:
        print("  skipped %s: %s" % (k, why))


@command("events", "events [seconds] [filter] [--poll]", "print what changes in the set while it happens: pushed by "
         "Live's listeners (song, tracks, mixer, every device parameter); --poll compares snapshots instead", "info")
def c_events(a):
    nums = [x for x in a if re.fullmatch(r"\d+(\.\d+)?", x)]
    sec = float(nums[0]) if nums else 30
    f = " ".join(x for x in a if x not in nums and not x.startswith("--")).lower()
    t_end = time.time() + sec
    if "--poll" not in a:
        try:
            st = call("events_start", timeout=30)
        except LiveError:
            st = None
        if st is not None:
            seq, last = st["seq"], {}
            try:
                while time.time() < t_end:
                    time.sleep(0.2)
                    r = call("events_poll", since=seq)
                    seq = r["seq"]
                    latest = {}
                    for e in r["events"]:               # a knob turn sends many values: keep the last one per key
                        latest[e["key"]] = e
                    for k, e in latest.items():
                        if (not f or f in e["label"].lower()) and last.get(k) != e["value"]:
                            print(json.dumps(e) if JSON else "%s %s: %s" % (time.strftime("%H:%M:%S"), e["label"], e["value"]))
                            last[k] = e["value"]
                    sys.stdout.flush()
            finally:
                call("events_stop")
            return
    prev = _take()
    prev_song = call("song")
    while time.time() < t_end:
        time.sleep(0.5)
        cur = _take()
        song = call("song")
        stamp = time.strftime("%H:%M:%S")
        for k in ("playing", "tempo", "record", "loop", "metronome", "tracks", "scenes"):
            if song.get(k) != prev_song.get(k):
                print("%s song %s: %s -> %s" % (stamp, k, prev_song.get(k), song.get(k)))
        changed, added, removed = _diff(prev, cur)
        for k, lab, ot, nt, _, _ in changed:
            if k != "song|tempo" and (not f or f in lab.lower()):
                print("%s %s: %s -> %s" % (stamp, lab, ot, nt))
        sys.stdout.flush()
        prev, prev_song = cur, song


# ================================================================== offline: read a .als without opening it
@command("als", "als <file.als>", "read a Live set without opening it: tempo, tracks, devices and plugins, clips", "other")
def c_als(a):
    need(a, 1, "als <file.als>")
    import gzip
    import xml.etree.ElementTree as ET
    root = ET.fromstring(gzip.open(a[0]).read())
    ls = root.find("LiveSet")
    main = ls.find("MainTrack")
    if main is None:
        main = ls.find("MasterTrack")       # Live 11 and older
    tempo = main.find(".//Tempo/Manual") if main is not None else None
    res = {"file": os.path.abspath(a[0]), "creator": root.get("Creator"),
           "tempo": float(tempo.get("Value")) if tempo is not None else None, "tracks": []}

    def first(el, *paths):
        for pth in paths:
            x = el.find(pth)
            if x is not None and x.get("Value"):
                return x.get("Value")
        return None

    friendly = {"Eq8": "EQ Eight", "GlueCompressor": "Glue Compressor", "ProxyAudioEffectDevice": "External Audio Effect",
                "ProxyInstrumentDevice": "External Instrument", "AudioEffectGroupDevice": "Audio Effect Rack",
                "InstrumentGroupDevice": "Instrument Rack", "DrumGroupDevice": "Drum Rack", "MidiEffectGroupDevice": "MIDI Effect Rack",
                "Compressor2": "Compressor", "StereoGain": "Utility", "AutoFilter": "Auto Filter", "Reverb": "Reverb",
                "Delay": "Delay", "Limiter": "Limiter", "Saturator": "Saturator", "MultibandDynamics": "Multiband Dynamics",
                "OriginalSimpler": "Simpler", "MultiSampler": "Sampler", "InstrumentVector": "Wavetable", "Drift": "Drift"}

    def devname(d):
        un = first(d, "UserName")
        if d.tag in ("PluginDevice", "AuPluginDevice"):
            return un or first(d, ".//PluginDesc//PlugName", ".//PluginDesc//Name") or d.tag
        preset = first(d, "LastPresetRef//RelativePath", "LastPresetRef//Path")
        base = friendly.get(d.tag, d.tag)
        if un:
            return un
        if preset:
            pn = os.path.splitext(os.path.basename(preset))[0]
            return base if pn == base else "%s (%s)" % (pn, base)
        return base

    for kind in ("AudioTrack", "MidiTrack", "GroupTrack", "ReturnTrack"):
        for t in ls.find("Tracks").findall(kind) if kind != "ReturnTrack" else ls.find("Tracks").findall("ReturnTrack"):
            name = t.find("Name/EffectiveName").get("Value")
            chain = t.find("DeviceChain/DeviceChain/Devices")
            devs = [devname(d) for d in list(chain)] if chain is not None else []
            n_sess = len(t.findall(".//ClipSlot//AudioClip")) + len(t.findall(".//ClipSlot//MidiClip"))
            n_arr = len(t.findall(".//ArrangerAutomation/Events/AudioClip")) + len(t.findall(".//ArrangerAutomation/Events/MidiClip"))
            res["tracks"].append({"kind": kind.replace("Track", "").lower(), "name": name, "devices": devs,
                                  "session_clips": n_sess, "arrangement_clips": n_arr})
    m = main.find("DeviceChain/DeviceChain/Devices") if main is not None else None
    res["master_devices"] = [devname(d) for d in list(m)] if m is not None else []
    if JSON:
        return out(res)
    print("%s (%s), tempo %s" % (res["file"], res["creator"], res["tempo"]))
    for t in res["tracks"]:
        print("  %-6s %-28s clips %3d/%-3d %s" % (t["kind"], t["name"][:28], t["session_clips"], t["arrangement_clips"],
              ", ".join(t["devices"])))
    print("  master: " + ", ".join(res["master_devices"]))


# ================================================================== plugin inventory
CATALOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.json")
INVENTORY = os.path.join(CONFIG_DIR, "plugin-inventory.json")
_VARIANT = re.compile(r"\s*\((m|s|mono|stereo|[^()]*->[^()]*|[0-9.]+/[0-9.]+)\)$|\s+(Mono/Stereo|Mono|Stereo)$", re.I)


def _base_name(n):
    prev = None
    while prev != n:
        prev, n = n, _VARIANT.sub("", n).strip()
    return n


def _classify(rules, vendor, name):
    key = "%s / %s" % (vendor, name)
    for r in rules:
        if re.search(r["match"], key, re.I):
            return {k: r[k] for k in ("category", "styles", "emulates", "notes") if k in r}
    return {"category": "uncategorized"}


def _inventory_from_live():
    items = call("plugin_list", natives=True, timeout=90)
    out_ = {}
    for x in items:
        parts = x["path"].split("/")
        if x.get("native"):
            vendor, fmt = "Live", "native"
        else:
            fmt = parts[1]
            vendor = parts[2] if len(parts) > 3 else "?"
        if vendor == "Elektron Music Machines":
            vendor = "Elektron"
        name = _base_name(x["name"])
        e = out_.setdefault((vendor, name), {"vendor": vendor, "name": name, "formats": [], "paths": {}})
        if fmt not in e["formats"]:
            e["formats"].append(fmt)
            e["paths"][fmt] = x["path"]
        if x.get("native"):
            e["kind"] = x["native"]
    return list(out_.values()), "live"


def _inventory_from_files():
    import plistlib
    out_ = {}
    roots = ["/Library/Audio/Plug-Ins", os.path.expanduser("~/Library/Audio/Plug-Ins")]
    for r in roots:
        for b in glob.glob(os.path.join(r, "Components", "*.component")):
            try:
                info = plistlib.load(open(os.path.join(b, "Contents", "Info.plist"), "rb"))
            except Exception:
                continue
            for comp in info.get("AudioComponents", []):
                full = comp.get("name", "")
                vendor, _, name = full.partition(":") if ":" in full else ("?", "", full)
                name = _base_name(name.strip())
                e = out_.setdefault((vendor.strip(), name), {"vendor": vendor.strip(), "name": name, "formats": [], "paths": {}})
                if "AUv2" not in e["formats"]:
                    e["formats"].append("AUv2")
                    e["paths"]["AUv2"] = b
        for b in glob.glob(os.path.join(r, "VST3", "*.vst3")):
            vendor, names = "?", [os.path.basename(b)[:-5]]
            mi = os.path.join(b, "Contents", "Resources", "moduleinfo.json")
            try:
                m = json.load(open(mi))
                vendor = m.get("Factory Info", {}).get("Vendor", vendor)
                names = [c["Name"] for c in m.get("Classes", []) if c.get("Category") == "Audio Module Class"] or names
            except Exception:
                pass
            for n in names:
                e = out_.setdefault((vendor, _base_name(n)), {"vendor": vendor, "name": _base_name(n), "formats": [], "paths": {}})
                if "VST3" not in e["formats"]:
                    e["formats"].append("VST3")
                    e["paths"]["VST3"] = b
    return list(out_.values()), "files"


def plugin_inventory(refresh=False):
    if not refresh and os.path.exists(INVENTORY) and time.time() - os.path.getmtime(INVENTORY) < 7 * 86400:
        return json.load(open(INVENTORY))
    try:
        items, source = _inventory_from_live()
    except LiveError:
        items, source = _inventory_from_files()
    rules = json.load(open(CATALOG))["rules"]
    user_cat = os.path.join(CONFIG_DIR, "catalog.json")
    if os.path.exists(user_cat):          # the user's own rules come first
        rules = json.load(open(user_cat)).get("rules", []) + rules
    for e in items:
        e.update(_classify(rules, e["vendor"], e["name"]))
    inv = {"time": time.strftime("%Y-%m-%d %H:%M"), "source": source, "plugins": items}
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(INVENTORY, "w") as fh:
        json.dump(inv, fh, ensure_ascii=False)
    return inv


@command("plugins", "plugins [query] [--refresh] [--native|--no-native]", "installed plugins and Live devices by "
         "category, with style and emulated hardware (catalog.json). query matches name, vendor, category, style: "
         "'compressor', 'opto', 'la-2a', 'plate', 'tape'...", "browser")
def c_plugins(a):
    flags = {x for x in a if x.startswith("--")}
    q = " ".join(x for x in a if not x.startswith("--")).lower()
    inv = plugin_inventory(refresh="--refresh" in flags)
    ps = inv["plugins"]
    if "--native" in flags:
        ps = [p_ for p_ in ps if p_["vendor"] == "Live"]
    if "--no-native" in flags:
        ps = [p_ for p_ in ps if p_["vendor"] != "Live"]
    if q:
        def hay(p_):
            return " ".join([p_["vendor"], p_["name"], p_.get("category", ""), " ".join(p_.get("styles", [])),
                             p_.get("emulates", ""), p_.get("notes", "")]).lower()
        words = q.replace("-", " ").split()
        ps = [p_ for p_ in ps if all(w in hay(p_).replace("-", " ") for w in words)]
    if JSON:
        return out({"time": inv["time"], "source": inv["source"], "plugins": ps})
    by = {}
    for p_ in ps:
        by.setdefault(p_.get("category", "uncategorized"), []).append(p_)
    print("%d plugins and devices (inventory %s, from %s)" % (len(ps), inv["time"], inv["source"]))
    for cat in sorted(by, key=lambda c: (c == "uncategorized", c)):
        print("\n%s (%d)" % (cat, len(by[cat])))
        for p_ in sorted(by[cat], key=lambda p_: (p_["vendor"] != "Live", p_["vendor"].lower(), p_["name"].lower())):
            extra = []
            if p_.get("emulates"):
                extra.append(p_["emulates"])
            if p_.get("styles") and q:
                extra.append(", ".join(p_["styles"]))
            print("  %-48s %-14s %s" % (("%s / %s" % (p_["vendor"], p_["name"]))[:48], "+".join(p_["formats"])[:14],
                  " | ".join(extra)[:90]))
            if q and p_.get("notes"):
                print("  %-48s note: %s" % ("", p_["notes"]))


# ================================================================== maintenance
@command("reload", "reload", "re-read commands.py inside Live (no restart)", "other", aliases=("ricarica",))
def c_reload(a):
    out(call("reload"))


@command("call", "call <cmd> [json]", "raw call to the remote script, e.g. live call tracks", "other")
def c_call(a):
    need(a, 1, "call <cmd> [json]")
    kw = json.loads(a[1]) if len(a) > 1 else {}
    print(json.dumps(call(a[0], **kw), indent=1, ensure_ascii=False))


GROUPS = [("info", "Info"), ("transport", "Transport and locators"), ("tracks", "Tracks and mixer"),
          ("devices", "Devices"), ("clips", "Scenes and clips"), ("browser", "Browser"), ("audio", "Audio"),
          ("probes", "Probes (Live Probe Protocol)"), ("ext", "Extensions"), ("other", "Other")]


@command("help", "help [command]", "this help", "other", aliases=("aiuto", "-h", "--help"))
def c_help(a):
    if a:
        name = ALIASES.get(a[0], a[0])
        if name not in COMMANDS:
            raise LiveError("unknown command: %s" % a[0])
        _, usage, h, _ = COMMANDS[name]
        print("live %s\n  %s" % (usage, h))
        return
    print("live: command line for Ableton Live (LiveCLI). Add --json to any command for machine output.\n"
          "<track>: m (master), t3, r0, return letter (A, B...) or part of the name.\n"
          "<device>: index, path (2.0.1 = device 2 -> chain 0 -> device 1) or part of the name.\n"
          "<probe>: #<probe id>, label, uid or track name.")
    for g, title in GROUPS:
        items = [(n, c) for n, c in COMMANDS.items() if c[3] == g]
        if not items:
            continue
        print("\n%s" % title)
        for n, (_, usage, h, _) in items:
            print("  %-52s %s" % (usage, h))


# ================================================================== extensions
def load_extensions():
    """Python files in ~/.config/live-cli/extensions/ (or listed in config 'extensions') can add commands:
    def register(cli): @cli.command("name", "usage", "help", "ext") def f(a): ..."""
    paths = sorted(glob.glob(os.path.join(CONFIG_DIR, "extensions", "*.py")))
    try:
        paths += [os.path.expanduser(p) for p in config().get("extensions", [])]
    except LiveError:
        pass
    mod_self = sys.modules[__name__]
    for p in paths:
        try:
            spec = importlib.util.spec_from_file_location("livecli_ext_" + os.path.basename(p)[:-3], p)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            if hasattr(m, "register"):
                m.register(mod_self)
        except Exception as e:  # a broken extension must not break the CLI
            print("live: extension %s not loaded: %s" % (p, e), file=sys.stderr)


def _load_modules():
    from . import cmd_setup, cmd_midi, cmd_offline, cmd_mix, cmd_hardware  # noqa: F401  (they register their commands)


def main(argv=None):
    global JSON
    _load_modules()
    a = list(sys.argv[1:] if argv is None else argv)
    if "--json" in a:
        JSON = True
        a.remove("--json")
    load_extensions()
    if not a:
        a = ["help"]
    name = ALIASES.get(a[0], a[0])
    if name not in COMMANDS:
        print("live: unknown command '%s' (live help)" % a[0], file=sys.stderr)
        return 2
    try:
        COMMANDS[name][0](a[1:])
    except LiveError as e:
        err = {"ok": False, "error": str(e), "code": e.code}
        if e.hint:
            err["hint"] = e.hint
        if JSON:
            print(json.dumps(err))
        else:
            print("live: %s" % e, file=sys.stderr)
            if e.hint:
                print("  hint: %s" % e.hint, file=sys.stderr)
        return EXIT_CODES.get(e.code, 1)
    except (OSError, ValueError) as e:
        msg = "connection problem: %s" % e
        print(json.dumps({"ok": False, "error": msg, "code": "error"}) if JSON else "live: " + msg,
              file=sys.stdout if JSON else sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
