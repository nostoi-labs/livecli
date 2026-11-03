"""live: a command line for Ableton Live, built for AI agents (short text output, --json everywhere)."""
import glob
import importlib.util
import json
import os
import statistics
import sys
import time

from . import probes as P
from .client import CONFIG_DIR, LiveError, call, config
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


@command("notes", "notes <track> <slot>", "MIDI notes of a session clip (JSON)", "info")
def c_notes(a):
    need(a, 2, "notes <track> <slot>")
    print(json.dumps(call("notes", track=track(a[0])["ref"], slot=int(a[1]))))


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


@command("set-notes", "set-notes <track> <slot> [--add] < notes.json", "write MIDI notes from stdin: "
         "[{\"pitch\":60,\"start\":0,\"duration\":0.5,\"velocity\":100}, ...]", "clips")
def c_set_notes(a):
    need(a, 2, "set-notes <track> <slot> [--add] < notes.json")
    notes = json.load(sys.stdin)
    out(call("set_notes", track=track(a[0])["ref"], slot=int(a[1]), notes=notes, add="--add" in a))


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
                  "Live's delay compensation is off by this much for this device"))
    print("  correlation %.2f, ambiguity %.2f, alignment %s" % (r.get("correlation_peak") or 0, r.get("ambiguity") or 0, r.get("aligned")))
    for w in warn:
        print("  warning: " + w)


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


def main(argv=None):
    global JSON
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
        if JSON:
            print(json.dumps({"ok": False, "error": str(e)}))
        else:
            print("live: %s" % e, file=sys.stderr)
        return 1
    except (OSError, ValueError) as e:
        msg = "connection problem: %s" % e
        print(json.dumps({"ok": False, "error": msg}) if JSON else "live: " + msg, file=sys.stdout if JSON else sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0
