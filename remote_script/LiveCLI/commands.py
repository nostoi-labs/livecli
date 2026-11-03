# LiveCLI commands. Reloadable at runtime with {"cmd": "reload"} (no Live restart needed).
#
# Each command is a function c_<name>(cs, req) that runs on Live's main thread and returns a JSON-able value.
# A command may return DEFERRED and answer later with box.put(...) (used when Live needs a tick in between).
#
# References used everywhere:
#   track:  "t3" (4th regular track), "r0" (first return track), "m" (master)
#   device: "2" (3rd device of the track) or a path "2.0.1" = device 2 -> chain 0 -> device 1 (racks)
#   time:   beats (quarter notes), as in Live's API
from __future__ import absolute_import, print_function, unicode_literals

import json
import os
import re

import Live

VERSION = 15
DEFERRED = object()


def run(cs, req, box):
    f = globals().get("c_" + str(req.get("cmd")))
    if f is None:
        raise ValueError("unknown command: %s" % req.get("cmd"))
    if getattr(f, "deferred", False):
        return f(cs, req, box)
    return f(cs, req)


def deferred(f):
    f.deferred = True
    return f


# ------------------------------------------------------------------ helpers
def _song(cs):
    return cs._song


def _config():
    p = os.path.expanduser("~/.config/live-cli/config.json")
    try:
        with open(p) as fh:
            return json.load(fh)
    except Exception:
        return {}


def _tracks(cs):
    s = _song(cs)
    out = [("t%d" % i, t) for i, t in enumerate(s.tracks)]
    out += [("r%d" % i, t) for i, t in enumerate(s.return_tracks)]
    out.append(("m", s.master_track))
    return out


def _track(cs, ref):
    s = _song(cs)
    ref = str(ref)
    try:
        if ref == "m":
            return s.master_track
        if ref[0] == "t":
            return s.tracks[int(ref[1:])]
        if ref[0] == "r":
            return s.return_tracks[int(ref[1:])]
    except (IndexError, ValueError):
        pass
    raise ValueError("bad track reference: %s (use t<N>, r<N> or m)" % ref)


def _resolve(cs, ref, path):
    """Return (container, index, device). The container is a track or a rack chain."""
    cont = _track(cs, ref)
    parts = [int(x) for x in str(path).split(".") if x != ""]
    d, i = None, 0
    while parts:
        i = parts.pop(0)
        d = cont.devices[i]
        if parts:
            cont = d.chains[parts.pop(0)]
    if d is None:
        raise ValueError("empty device path")
    return cont, i, d


def _dev_on(d):
    p = d.parameters[0] if len(d.parameters) else None
    return bool(p.value) if p is not None and p.name == "Device On" else None


def _dev_info(d, path):
    info = {"i": int(path.split(".")[-1]), "path": path, "name": d.name, "class": d.class_name,
            "type": str(d.type), "on": _dev_on(d)}
    try:
        info["latency_samples"] = d.latency_in_samples
        info["latency_ms"] = round(d.latency_in_ms, 3)
    except Exception:
        pass
    try:
        if d.can_compare_ab:
            info["ab"] = "B" if d.is_using_compare_preset_b else "A"
    except Exception:
        pass
    if d.can_have_chains:
        info["chains"] = [{"name": ch.name,
                           "devices": [_dev_info(x, "%s.%d.%d" % (path, c, j)) for j, x in enumerate(ch.devices)]}
                          for c, ch in enumerate(d.chains)]
    return info


def _name(obj, attr):
    try:
        return getattr(obj, attr).display_name
    except Exception:
        return None


def _color(obj):
    try:
        return "#%06X" % (int(obj.color) & 0xFFFFFF)
    except Exception:
        return None


def _beats_per_bar(s):
    return s.signature_numerator * 4.0 / s.signature_denominator


def _bar(s, beats):
    return beats / _beats_per_bar(s) + 1


def _num(txt):
    t = str(txt).strip().lower()
    if "inf" in t:
        return float("-inf") if "-" in t else float("inf")
    mk = re.search(r"(-?\d+)k(\d+)", t)  # soothe style: 2k5 = 2500
    if mk:
        return float(mk.group(1) + "." + mk.group(2)) * 1000
    m = re.search(r"-?\d+(?:[.,]\d+)?", t)
    if not m:
        return None
    v = float(m.group(0).replace(",", "."))
    if re.search(r"\d\s*ms\b", t):     # times in seconds as the base unit: '600 ms' < '1.00 s'
        return v / 1000.0
    if re.search(r"\d\s*k(hz)?\b", t):
        v *= 1000
    return v


def _value_for_text(p, text):
    """Raw value whose display text shows `text` ('-3 dB', '250 Hz', '20L', 'Bell'), without touching the parameter.
    Works for Live's own devices (str_for_value). Plugins don't translate values in advance: the CLI uses read-back."""
    if p.is_quantized:
        choices = [p.str_for_value(v) for v in range(int(p.min), int(p.max) + 1)]
        for k, c in enumerate(choices):
            if str(text).strip().lower() == c.strip().lower():
                return int(p.min) + k
        raise ValueError("'%s' is not one of: %s" % (text, ", ".join(choices)))
    pan = re.match(r"^\s*(?:(\d+(?:[.,]\d+)?)\s*([LR])|C|CENTER|CENTRE)\s*$", str(text).strip().upper())
    if pan and p.min < 0 < p.max and str(p.str_for_value(p.min)).strip().upper().endswith("L"):
        if pan.group(1) is None:
            return 0.0
        n = float(pan.group(1).replace(",", ".")) / 50.0
        return max(p.min, min(p.max, -n * abs(p.min) if pan.group(2) == "L" else n * p.max))
    goal = _num(text)
    if goal is None:
        raise ValueError("not a number: %s" % text)
    lo, hi = p.min, p.max
    a, b = _num(p.str_for_value(lo)), _num(p.str_for_value(hi))
    if a is None or b is None:
        raise ValueError("parameter %s has no readable numeric values" % p.name)
    up = b >= a
    for _ in range(60):
        mid = (lo + hi) / 2.0
        v = _num(p.str_for_value(mid))
        if v is None:
            break
        if (v < goal) == up:
            lo = mid
        else:
            hi = mid
    # the display is rounded: of the values around the boundary, keep the one that shows the closest number
    cands = [lo, (lo + hi) / 2.0, hi]
    return min(cands, key=lambda x: (abs((_num(p.str_for_value(x)) or 0) - goal), abs(x - (lo + hi) / 2.0)))


def _set_by_text(p, text):
    p.value = _value_for_text(p, text)


def _set_param(p, req):
    before = str(p)
    if req.get("text") is not None:
        _set_by_text(p, req["text"])
    else:
        p.value = max(p.min, min(p.max, float(req["value"])))
    return {"parameter": p.name, "before": before, "after": str(p)}


def _param_info(i, p):
    return {"i": i, "name": p.name, "value": p.value, "text": str(p), "min": p.min, "max": p.max,
            "quantized": p.is_quantized, "enabled": p.is_enabled}


LAUNCH_MODES = ["trigger", "gate", "toggle", "repeat"]
LAUNCH_Q = ["global", "none", "8 bars", "4 bars", "2 bars", "1 bar", "1/2", "1/2T", "1/4", "1/4T", "1/8", "1/8T",
            "1/16", "1/16T", "1/32"]
WARP_MODES = ["beats", "tones", "texture", "re-pitch", "complex", "rex", "complex pro"]
QUANT_GRID = {"1/4": 1, "1/8": 2, "1/8T": 3, "1/8+T": 4, "1/16": 5, "1/16T": 6, "1/16+T": 7, "1/32": 8}


def _safe(obj, attr, default=None):
    try:
        return getattr(obj, attr)
    except Exception:
        return default


def _clip_info(c):
    e = {"name": c.name, "color": _color(c), "length": c.length, "looping": c.looping,
         "loop_start": c.loop_start, "loop_end": c.loop_end, "start_marker": _safe(c, "start_marker"),
         "end_marker": _safe(c, "end_marker"), "midi": c.is_midi_clip,
         "playing": c.is_playing, "triggered": c.is_triggered, "muted": c.muted,
         "launch_mode": LAUNCH_MODES[c.launch_mode] if _safe(c, "launch_mode") is not None else None,
         "launch_quantization": LAUNCH_Q[c.launch_quantization] if _safe(c, "launch_quantization") is not None else None,
         "legato": _safe(c, "legato"), "velocity_amount": _safe(c, "velocity_amount"),
         "has_envelopes": _safe(c, "has_envelopes")}
    if _safe(c, "is_arrangement_clip"):
        e.update({"start": c.start_time, "end": c.end_time})
    if c.is_audio_clip:
        wm = _safe(c, "warp_mode")
        e.update({"file": _safe(c, "file_path"), "warping": _safe(c, "warping"),
                  "warp_mode": WARP_MODES[wm] if wm is not None and wm < len(WARP_MODES) else wm,
                  "gain": _safe(c, "gain_display_string"), "pitch_coarse": _safe(c, "pitch_coarse"),
                  "pitch_fine": _safe(c, "pitch_fine"), "ram_mode": _safe(c, "ram_mode"),
                  "sample_rate": _safe(c, "sample_rate"), "sample_length": _safe(c, "sample_length")})
    return e


def _clip(cs, req):
    """Session clip (track + slot) or arrangement clip (track + arr = index in arrangement order)."""
    t = _track(cs, req["track"])
    if req.get("arr") is not None:
        clips = sorted(t.arrangement_clips, key=lambda c: c.start_time)
        i = int(req["arr"])
        if i >= len(clips):
            raise ValueError("%s has %d arrangement clips" % (t.name, len(clips)))
        return t, None, clips[i]
    slot = t.clip_slots[int(req["slot"])]
    if not slot.has_clip:
        raise ValueError("no clip in %s slot %s" % (t.name, req["slot"]))
    return t, slot, slot.clip


def _scene_ref(cs, q):
    s = _song(cs)
    return s.scenes[int(q)]


# ------------------------------------------------------------------ info
def c_ping(cs, req):
    return {"pong": True, "version": VERSION}


def c_cpu(cs, req):
    app = Live.Application.get_application()
    out = {"unit": "percent"}
    for k in ("average_process_usage", "peak_process_usage"):
        try:
            out[k] = round(getattr(app, k), 1)
        except Exception:
            out[k] = None
    return out


def c_song(cs, req):
    s = _song(cs)
    app = Live.Application.get_application()
    out = {"tempo": s.tempo, "signature": "%d/%d" % (s.signature_numerator, s.signature_denominator),
           "playing": s.is_playing, "position": s.current_song_time, "bar": _bar(s, s.current_song_time),
           "loop": s.loop, "loop_start": s.loop_start, "loop_length": s.loop_length,
           "metronome": s.metronome, "record": s.record_mode, "session_record": s.session_record,
           "overdub": s.arrangement_overdub, "punch_in": s.punch_in, "punch_out": s.punch_out,
           "tracks": len(s.tracks), "returns": len(s.return_tracks), "scenes": len(s.scenes),
           "can_undo": s.can_undo, "can_redo": s.can_redo,
           "live": "%d.%d.%d" % (app.get_major_version(), app.get_minor_version(), app.get_bugfix_version()),
           "version": VERSION}
    for k in ("root_note", "scale_name", "song_length", "last_event_time"):
        try:
            out[k] = getattr(s, k)
        except Exception:
            pass
    try:
        out["scale_intervals"] = [int(x) for x in s.scale_intervals]
    except Exception:
        pass
    out["file_path"] = _safe(s, "file_path") or None
    return out


def c_set_song(cs, req):
    s = _song(cs)
    if "tempo" in req:
        s.tempo = float(req["tempo"])
    if "signature" in req:
        n, d = str(req["signature"]).split("/")
        s.signature_numerator, s.signature_denominator = int(n), int(d)
    for k in ("metronome", "loop", "punch_in", "punch_out"):
        if k in req:
            setattr(s, k, bool(req[k]))
    if "overdub" in req:
        s.arrangement_overdub = bool(req["overdub"])
    if "loop_start" in req:
        s.loop_start = float(req["loop_start"])
    if "loop_length" in req:
        s.loop_length = float(req["loop_length"])
    if "position" in req:
        s.current_song_time = float(req["position"])
    if req.get("back_to_arranger"):
        # Live's "Back to Arrangement": every track that deviates goes back to playing the arrangement
        for t in s.tracks:
            if getattr(t, "back_to_arranger", False):
                t.back_to_arranger = False
        s.back_to_arranger = False
    if "root_note" in req:
        s.root_note = int(req["root_note"])
    if "scale_name" in req:
        s.scale_name = str(req["scale_name"])
    return c_song(cs, req)


def c_tracks(cs, req):
    s = _song(cs)
    out = []
    for ref, t in _tracks(cs):
        md = t.mixer_device
        e = {"ref": ref, "name": t.name, "color": _color(t), "volume": str(md.volume),
             "cpu_impact": round(getattr(t, "performance_impact", 0.0) or 0.0, 4),
             "pan": str(md.panning),
             "devices": [_dev_info(d, str(j)) for j, d in enumerate(t.devices)]}
        if ref != "m":
            e.update({"mute": t.mute, "solo": t.solo, "output": _name(t, "output_routing_type"),
                      "output_channel": _name(t, "output_routing_channel")})
        if ref[0] == "t":
            e.update({"kind": "group" if t.is_foldable else ("midi" if t.has_midi_input else "audio"),
                      "arm": t.arm if t.can_be_armed else None,
                      "implicit_arm": _safe(t, "implicit_arm") if t.can_be_armed else None,
                      "input": _name(t, "input_routing_type"), "input_channel": _name(t, "input_routing_channel"),
                      "monitor": ["in", "auto", "off"][t.current_monitoring_state] if t.can_be_armed else None,
                      "group": t.group_track.name if t.is_grouped else None,
                      "frozen": getattr(t, "is_frozen", None)})
        out.append(e)
    return out


def c_params(cs, req):
    _, _, d = _resolve(cs, req["track"], req["device"])
    return {"device": d.name, "class": d.class_name,
            "params": [_param_info(i, p) for i, p in enumerate(d.parameters)]}


def c_mixer(cs, req):
    t = _track(cs, req["track"])
    md = t.mixer_device
    rets = _song(cs).return_tracks
    return {"track": t.name, "volume": str(md.volume), "pan": str(md.panning),
            "sends": [{"i": i, "return": rets[i].name if i < len(rets) else "?", "value": str(x)}
                      for i, x in enumerate(md.sends)]}


def c_meters(cs, req):
    """Instant output peaks of every track (Live's meter scale, 0..1)."""
    out = {}
    for ref, t in _tracks(cs):
        try:
            out[ref] = [round(t.output_meter_left, 4), round(t.output_meter_right, 4)]
        except Exception:
            pass
    return out


def c_routing(cs, req):
    t = _track(cs, req["track"])
    r = {"output": _name(t, "output_routing_type"), "output_channel": _name(t, "output_routing_channel"),
         "output_types": [x.display_name for x in t.available_output_routing_types],
         "output_channels": [x.display_name for x in t.available_output_routing_channels]}
    if str(req["track"]).startswith("t"):
        r.update({"input": _name(t, "input_routing_type"), "input_channel": _name(t, "input_routing_channel"),
                  "input_types": [x.display_name for x in t.available_input_routing_types],
                  "input_channels": [x.display_name for x in t.available_input_routing_channels]})
    return r


def c_locators(cs, req):
    s = _song(cs)
    return [{"time": c.time, "bar": _bar(s, c.time), "name": c.name} for c in s.cue_points]


def c_scenes(cs, req):
    return [{"i": i, "name": sc.name, "color": _color(sc),
             "tempo": sc.tempo if getattr(sc, "tempo_enabled", sc.tempo > 0) else None,
             "triggered": sc.is_triggered}
            for i, sc in enumerate(_song(cs).scenes)]


def c_clips(cs, req):
    """Session clips of one track (or of every regular track)."""
    s = _song(cs)
    refs = [req["track"]] if req.get("track") else ["t%d" % i for i in range(len(s.tracks))]
    out = []
    for ref in refs:
        t = _track(cs, ref)
        for i, slot in enumerate(t.clip_slots):
            if slot.has_clip:
                e = _clip_info(slot.clip)
                e.update({"track": ref, "track_name": t.name, "slot": i})
                out.append(e)
    return out


def c_arrangement(cs, req):
    """Arrangement clips of one track (or of every regular track)."""
    s = _song(cs)
    refs = [req["track"]] if req.get("track") else ["t%d" % i for i in range(len(s.tracks))]
    out = []
    for ref in refs:
        t = _track(cs, ref)
        for k, c in enumerate(sorted(getattr(t, "arrangement_clips", []), key=lambda c: c.start_time)):
            out.append({"track": ref, "track_name": t.name, "arr": k, "name": c.name, "start": c.start_time,
                        "end": c.end_time, "start_bar": _bar(s, c.start_time), "end_bar": _bar(s, c.end_time),
                        "muted": c.muted, "midi": c.is_midi_clip,
                        "file": getattr(c, "file_path", None) if c.is_audio_clip else None})
    return out


def c_notes(cs, req):
    _, _, c = _clip(cs, req)
    if not c.is_midi_clip:
        raise ValueError("not a MIDI clip")
    notes = c.get_all_notes_extended() if hasattr(c, "get_all_notes_extended") else c.get_notes_extended(0, 128, 0.0, 1e6)
    return [{"pitch": n.pitch, "start": n.start_time, "duration": n.duration, "velocity": n.velocity,
             "mute": n.mute, "probability": n.probability} for n in notes]


def _lom_object(cs, req):
    """Resolve 'song', 'app', 'view', or a track/device/mixer/clip reference to a Live object."""
    what = req.get("what", "song")
    s = _song(cs)
    if what == "song":
        return s
    if what == "app":
        return Live.Application.get_application()
    if what == "view":
        return s.view
    if what == "appview":
        return Live.Application.get_application().view
    if what == "track":
        return _track(cs, req["track"])
    if what == "mixer":
        return _track(cs, req["track"]).mixer_device
    if what == "device":
        return _resolve(cs, req["track"], req["device"])[2]
    if what == "param":
        return _resolve(cs, req["track"], req["device"])[2].parameters[int(req["param"])]
    if what == "clip":
        return _clip(cs, req)[2]
    if what == "arrangement_clip":
        return list(_track(cs, req["track"]).arrangement_clips)[int(req["index"])]
    if what == "scene":
        return _scene_ref(cs, req["scene"])
    raise ValueError("what = song | app | view | appview | track | mixer | device | param | clip | arrangement_clip | scene")


def _short(v):
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    if isinstance(v, str):
        return v[:200]
    try:
        n = len(v)
        return "<%s, %d items>" % (type(v).__name__, n)
    except Exception:
        pass
    return "<%s>" % type(v).__name__


def c_inspect(cs, req):
    """Discover what Live's API offers on an object: properties with their current value, and methods.
    Read-only: calls nothing, only reads attributes."""
    obj = _lom_object(cs, req)
    props, methods = {}, []
    for name in sorted(dir(obj)):
        if name.startswith("_") or name.endswith("_has_listener") or name.startswith("add_") or name.startswith("remove_"):
            continue
        try:
            v = getattr(obj, name)
        except Exception as e:
            props[name] = "<error: %s>" % type(e).__name__
            continue
        if callable(v):
            methods.append(name)
        else:
            props[name] = _short(v)
    return {"type": type(obj).__name__, "properties": props, "methods": methods}


# ------------------------------------------------------------------ browser
_ROOTS = ("user_library", "audio_effects", "instruments", "midi_effects", "plugins",
          "max_for_live", "drums", "sounds", "samples", "clips", "packs", "current_project")
_EXT = re.compile(r"\.(adg|adv|amxd|aupreset|vstpreset|fxp|alc|als|wav|aif|aiff)$", re.I)


def _search(item, q, path, out, depth, budget, limit):
    if depth > 9 or budget[0] <= 0 or len(out) >= limit:
        return
    for ch in item.children:
        budget[0] -= 1
        p = path + "/" + ch.name
        if q in ch.name.lower() and ch.is_loadable:
            out.append(p)
        if ch.is_folder or (not ch.is_loadable and len(ch.children)):
            _search(ch, q, p, out, depth + 1, budget, limit)


def c_search(cs, req):
    br = Live.Application.get_application().browser
    q = req["q"].lower()
    out = []
    for r in req.get("roots") or ("user_library", "audio_effects", "instruments", "plugins"):
        _search(getattr(br, r), q, r, out, 0, [int(req.get("budget", 40000))], int(req.get("limit", 60)))
    return out


def _item(br, path):
    parts = [p for p in path.split("/") if p]
    it = getattr(br, parts[0])
    for name in parts[1:]:
        nxt = next((ch for ch in it.children
                    if ch.name == name or _EXT.sub("", ch.name) == _EXT.sub("", name)), None)
        if nxt is None:
            raise ValueError("not found in browser: %s (inside %s)" % (name, it.name))
        it = nxt
    return it


def _walk_plugins(item, path, out, depth):
    if depth > 6:
        return
    for ch in item.children:
        p = path + "/" + ch.name
        if ch.is_loadable and not ch.is_folder:
            out.append({"path": p, "name": ch.name, "device": bool(_safe(ch, "is_device", False))})
        elif ch.is_folder or len(ch.children):
            _walk_plugins(ch, p, out, depth + 1)


def c_plugin_list(cs, req):
    """Every plugin Live knows (browser 'plugins': format/vendor/name) and, with natives=true, Live's own devices."""
    br = Live.Application.get_application().browser
    out = []
    _walk_plugins(br.plugins, "plugins", out, 0)
    if req.get("natives"):
        for root in ("audio_effects", "instruments", "midi_effects"):
            for ch in getattr(br, root).children:
                if _safe(ch, "is_device", False) or ch.is_loadable:
                    out.append({"path": "%s/%s" % (root, ch.name), "name": ch.name, "device": True, "native": root})
    return out


def c_browse(cs, req):
    """List the children of a browser folder, e.g. 'plugins/VST3'."""
    br = Live.Application.get_application().browser
    if not req.get("path"):
        return list(_ROOTS)
    it = _item(br, req["path"])
    return [{"name": ch.name, "folder": ch.is_folder, "loadable": ch.is_loadable} for ch in it.children]


# ------------------------------------------------------------------ transport
def c_play(cs, req):
    _song(cs).start_playing()
    return True


def c_continue(cs, req):
    _song(cs).continue_playing()
    return True


def c_stop(cs, req):
    _song(cs).stop_playing()
    return True


def c_stop_clips(cs, req):
    _song(cs).stop_all_clips()
    return True


def c_record(cs, req):
    s = _song(cs)
    s.record_mode = bool(req.get("on", not s.record_mode))
    return s.record_mode


def c_undo(cs, req):
    s = _song(cs)
    if not s.can_undo:
        raise ValueError("nothing to undo")
    s.undo()
    return True


def c_redo(cs, req):
    s = _song(cs)
    if not s.can_redo:
        raise ValueError("nothing to redo")
    s.redo()
    return True


def c_tap_tempo(cs, req):
    _song(cs).tap_tempo()
    return _song(cs).tempo


def c_capture_midi(cs, req):
    _song(cs).capture_midi()
    return True


def c_jump(cs, req):
    _song(cs).current_song_time = float(req["time"])
    return _song(cs).current_song_time


@deferred
def c_locator_set(cs, req, box):
    """Create (or rename) a locator at `time` beats. Live creates cue points only with the transport
    stopped and after a tick, so this stops, jumps, waits, creates, renames, then restores."""
    s = _song(cs)
    t = float(req["time"])
    was_playing, pos = s.is_playing, s.current_song_time
    s.stop_playing()

    def guard(fn):
        def g():
            try:
                fn()
            except Exception as e:
                box.put({"ok": False, "error": "%s: %s" % (type(e).__name__, e)})
        return g

    @guard
    def step1():
        s.current_song_time = t
        cs.livecli_after(2, step2)

    @guard
    def step2():
        if not any(abs(c.time - t) < 0.01 for c in s.cue_points):
            s.set_or_delete_cue()
        cs.livecli_after(1, step3)

    @guard
    def step3():
        cp = next((c for c in s.cue_points if abs(c.time - t) < 0.01), None)
        if cp is not None and req.get("name") is not None:
            cp.name = req["name"]
        s.current_song_time = pos
        if was_playing:
            s.start_playing()
        box.put({"ok": cp is not None, "result": c_locators(cs, req),
                 "error": None if cp is not None else "Live did not create the locator"})

    cs.livecli_after(2, step1)
    return DEFERRED


def c_locator_delete(cs, req):
    s = _song(cs)
    t = float(req["time"])
    cp = next((c for c in s.cue_points if abs(c.time - t) < 0.01), None)
    if cp is None:
        raise ValueError("no locator at %s" % t)
    s.stop_playing()
    s.current_song_time = t
    s.set_or_delete_cue()
    return True


def c_locator_rename(cs, req):
    t = float(req["time"])
    cp = next((c for c in _song(cs).cue_points if abs(c.time - t) < 0.01), None)
    if cp is None:
        raise ValueError("no locator at %s" % t)
    cp.name = req["name"]
    return c_locators(cs, req)


# ------------------------------------------------------------------ tracks
def c_track_create(cs, req):
    s = _song(cs)
    kind = req.get("kind", "audio")
    idx = int(req.get("index", -1))
    if kind == "audio":
        s.create_audio_track(idx)
    elif kind == "midi":
        s.create_midi_track(idx)
    elif kind == "return":
        s.create_return_track()
    else:
        raise ValueError("kind = audio | midi | return")
    if kind == "return":
        t, ref = s.return_tracks[-1], "r%d" % (len(s.return_tracks) - 1)
    else:
        i = idx if idx >= 0 else len(s.tracks) - 1
        t, ref = s.tracks[i], "t%d" % i
    if req.get("name"):
        t.name = req["name"]
    return {"ref": ref, "name": t.name}


def c_track_delete(cs, req):
    s = _song(cs)
    ref = str(req["track"])
    t = _track(cs, ref)
    name = t.name
    if ref[0] == "t":
        s.delete_track(int(ref[1:]))
    elif ref[0] == "r":
        s.delete_return_track(int(ref[1:]))
    else:
        raise ValueError("the master track cannot be deleted")
    return {"deleted": name}


def c_track_duplicate(cs, req):
    ref = str(req["track"])
    if ref[0] != "t":
        raise ValueError("only regular tracks can be duplicated")
    _song(cs).duplicate_track(int(ref[1:]))
    return {"ref": "t%d" % (int(ref[1:]) + 1)}


def c_track_set(cs, req):
    t = _track(cs, req["track"])
    if "name" in req:
        t.name = req["name"]
    if "color" in req:
        t.color = int(str(req["color"]).lstrip("#"), 16)
    for k in ("mute", "solo", "arm"):
        if k in req:
            setattr(t, k, bool(req[k]))
    if "monitor" in req:
        t.current_monitoring_state = ["in", "auto", "off"].index(req["monitor"])
    if "fold" in req and t.is_foldable:
        t.fold_state = bool(req["fold"])
    return {"track": t.name, "mute": getattr(t, "mute", None), "solo": getattr(t, "solo", None),
            "arm": t.arm if getattr(t, "can_be_armed", False) else None, "color": _color(t)}


def c_set_mixer(cs, req):
    t = _track(cs, req["track"])
    md = t.mixer_device
    what = req["what"]
    if what == "volume":
        p = md.volume
    elif what == "pan":
        p = md.panning
    elif what == "send":
        p = md.sends[int(req["index"])]
    else:
        raise ValueError("what = volume | pan | send")
    r = _set_param(p, req)
    r["track"] = t.name
    return r


def _pick(avail, name, what):
    x = [r for r in avail if r.display_name == name] or [r for r in avail if r.display_name.lower() == str(name).lower()]
    if not x:
        raise ValueError("%s not available: %s (choices: %s)" % (what, name, ", ".join(r.display_name for r in avail)))
    return x[0]


def c_set_routing(cs, req):
    t = _track(cs, req["track"])
    side = req.get("side", "output")
    out = {"before": [_name(t, side + "_routing_type"), _name(t, side + "_routing_channel")]}
    if req.get("type"):
        setattr(t, side + "_routing_type", _pick(getattr(t, "available_%s_routing_types" % side), req["type"], side + " type"))
    if req.get("channel"):
        setattr(t, side + "_routing_channel",
                _pick(getattr(t, "available_%s_routing_channels" % side), req["channel"], side + " channel"))
    out["after"] = [_name(t, side + "_routing_type"), _name(t, side + "_routing_channel")]
    return out


def c_select(cs, req):
    s = _song(cs)
    if req.get("track") is not None:
        s.view.selected_track = _track(cs, req["track"])
    if req.get("device") is not None:
        _, _, d = _resolve(cs, req["track"], req["device"])
        s.view.select_device(d)
    if req.get("scene") is not None:
        s.view.selected_scene = _scene_ref(cs, req["scene"])
    return True


# ------------------------------------------------------------------ devices
def c_device_on(cs, req):
    _, _, d = _resolve(cs, req["track"], req["device"])
    p = d.parameters[0]
    if p.name != "Device On":
        raise ValueError("%s has no Device On switch" % d.name)
    before = bool(p.value)
    p.value = p.max if req["on"] else p.min
    return {"device": d.name, "before": before, "after": bool(p.value)}


def c_set_param(cs, req):
    _, _, d = _resolve(cs, req["track"], req["device"])
    r = _set_param(d.parameters[int(req["param"])], req)
    r["device"] = d.name
    return r


def c_load(cs, req):
    """Load a browser item on a track. With 'after', insert it after that device."""
    s = _song(cs)
    br = Live.Application.get_application().browser
    it = _item(br, req["path"])
    if not it.is_loadable:
        raise ValueError("not loadable: %s" % req["path"])
    t = _track(cs, req["track"])
    s.view.selected_track = t
    if req.get("after") is not None:
        _, _, d = _resolve(cs, req["track"], req["after"])
        s.view.select_device(d)
    elif len(t.devices):
        s.view.select_device(t.devices[len(t.devices) - 1])
    br.load_item(it)
    return {"loaded": it.name, "devices": [d.name for d in t.devices]}


def c_delete_device(cs, req):
    cont, i, d = _resolve(cs, req["track"], req["device"])
    name = d.name
    cont.delete_device(i)
    return {"deleted": name, "devices": [x.name for x in cont.devices]}


def c_move_device(cs, req):
    s = _song(cs)
    cont, i, d = _resolve(cs, req["track"], req["device"])
    s.move_device(d, cont, int(req["position"]))
    return {"moved": d.name, "devices": [x.name for x in cont.devices]}


def c_rename_device(cs, req):
    _, _, d = _resolve(cs, req["track"], req["device"])
    d.name = req["name"]
    return {"device": d.name}


def c_ab(cs, req):
    """Live 12 device A/B compare: show, switch to a|b, or save the current state into the other slot."""
    _, _, d = _resolve(cs, req["track"], req["device"])
    if not getattr(d, "can_compare_ab", False):
        raise ValueError("%s has no A/B compare" % d.name)
    op = req.get("op")
    if op in ("a", "b"):
        d.is_using_compare_preset_b = (op == "b")
    elif op == "save":
        d.save_preset_to_compare_ab_slot()
    elif op == "toggle":
        d.is_using_compare_preset_b = not d.is_using_compare_preset_b
    return {"device": d.name, "using": "B" if d.is_using_compare_preset_b else "A"}


def c_undo_begin(cs, req):
    _song(cs).begin_undo_step()
    return True


def c_undo_end(cs, req):
    _song(cs).end_undo_step()
    return True


# ------------------------------------------------------------------ scenes and clips
def c_scene_fire(cs, req):
    _scene_ref(cs, req["scene"]).fire()
    return True


def c_scene_create(cs, req):
    s = _song(cs)
    idx = int(req.get("index", -1))
    s.create_scene(idx)
    i = idx if idx >= 0 else len(s.scenes) - 1
    if req.get("name"):
        s.scenes[i].name = req["name"]
    return {"i": i, "name": s.scenes[i].name}


def c_scene_set(cs, req):
    sc = _scene_ref(cs, req["scene"])
    if "name" in req:
        sc.name = req["name"]
    if "tempo" in req:
        sc.tempo = float(req["tempo"])
    return {"name": sc.name}


def c_scene_delete(cs, req):
    _song(cs).delete_scene(int(req["scene"]))
    return True


def c_scene_duplicate(cs, req):
    _song(cs).duplicate_scene(int(req["scene"]))
    return True


def c_clip_fire(cs, req):
    t = _track(cs, req["track"])
    t.clip_slots[int(req["slot"])].fire()
    return True


def c_clip_stop(cs, req):
    t = _track(cs, req["track"])
    if req.get("slot") is None:
        t.stop_all_clips()
    else:
        t.clip_slots[int(req["slot"])].stop()
    return True


def c_clip_create(cs, req):
    t = _track(cs, req["track"])
    slot = t.clip_slots[int(req["slot"])]
    if slot.has_clip:
        raise ValueError("slot %s already has a clip" % req["slot"])
    slot.create_clip(float(req.get("length", 4.0)))
    if req.get("name"):
        slot.clip.name = req["name"]
    return _clip_info(slot.clip)


def c_clip_delete(cs, req):
    t, slot, c = _clip(cs, req)
    name = c.name
    if slot is None:
        t.delete_clip(c)
    else:
        slot.delete_clip()
    return {"deleted": name}


def c_clip_duplicate(cs, req):
    t = _track(cs, req["track"])
    i = t.duplicate_clip_slot(int(req["slot"]))
    return {"slot": i}


def _set_by_display(get_text, set_raw, lo, hi, goal):
    """Binary search a raw value so that the display text shows `goal` (for values without str_for_value)."""
    a, b = lo, hi
    set_raw(a)
    va = _num(get_text())
    set_raw(b)
    vb = _num(get_text())
    up = (vb or 0) >= (va or 0)
    for _ in range(40):
        m = (a + b) / 2.0
        set_raw(m)
        v = _num(get_text())
        if v is None:
            break
        if (v < goal) == up:
            a = m
        else:
            b = m
    cands = [a, (a + b) / 2.0, b]
    best = None
    for x in cands:
        set_raw(x)
        d = abs((_num(get_text()) or 0) - goal)
        if best is None or d < best[0]:
            best = (d, x)
    set_raw(best[1])


def c_clip_set(cs, req):
    """Clip properties. launch_mode: trigger|gate|toggle|repeat; launch_quantization: global|none|1 bar|1/4...;
    warp_mode: beats|tones|texture|re-pitch|complex|complex pro; gain: '-3 dB' or raw 0..1."""
    _, _, c = _clip(cs, req)
    if "name" in req:
        c.name = req["name"]
    if "color" in req:
        c.color = int(str(req["color"]).lstrip("#"), 16)
    for k in ("looping", "muted", "legato", "warping", "ram_mode"):
        if k in req:
            setattr(c, k, bool(req[k]))
    # markers: set in a safe order (loop_end before loop_start when moving right, etc.)
    for k in ("loop_end", "loop_start", "end_marker", "start_marker") if float(req.get("loop_start", 0)) >= c.loop_end else \
            ("loop_start", "loop_end", "start_marker", "end_marker"):
        if k in req:
            setattr(c, k, float(req[k]))
    if "launch_mode" in req:
        c.launch_mode = LAUNCH_MODES.index(req["launch_mode"])
    if "launch_quantization" in req:
        c.launch_quantization = LAUNCH_Q.index(req["launch_quantization"])
    if "velocity_amount" in req:
        c.velocity_amount = float(req["velocity_amount"])
    if "warp_mode" in req:
        c.warp_mode = WARP_MODES.index(req["warp_mode"])
    if "pitch_coarse" in req:
        c.pitch_coarse = int(req["pitch_coarse"])
    if "pitch_fine" in req:
        c.pitch_fine = float(req["pitch_fine"])
    if "gain" in req:
        g = str(req["gain"])
        if g.startswith("="):
            c.gain = float(g[1:])
        else:
            _set_by_display(lambda: c.gain_display_string, lambda v: setattr(c, "gain", v), 0.0, 1.0, _num(g))
    return _clip_info(c)


def c_clip_edit(cs, req):
    """MIDI/clip editing: quantize (grid, amount), transpose (semitones), crop, duplicate_loop, velocity (scale %)."""
    _, _, c = _clip(cs, req)
    op = req["op"]
    if op == "quantize":
        c.quantize(QUANT_GRID[req.get("grid", "1/16")], float(req.get("amount", 1.0)))
    elif op == "crop":
        c.crop()
    elif op == "duplicate_loop":
        c.duplicate_loop()
    elif op in ("transpose", "velocity"):
        notes = c.get_all_notes_extended() if hasattr(c, "get_all_notes_extended") else c.get_notes_extended(0, 128, 0.0, 1e6)
        for n in notes:
            if op == "transpose":
                n.pitch = max(0, min(127, n.pitch + int(req["semitones"])))
            else:
                n.velocity = max(1.0, min(127.0, n.velocity * float(req["percent"]) / 100.0))
        c.apply_note_modifications(notes)
    else:
        raise ValueError("op = quantize | transpose | velocity | crop | duplicate_loop")
    return _clip_info(c)


def c_warp_markers(cs, req):
    _, _, c = _clip(cs, req)
    if not c.is_audio_clip:
        raise ValueError("not an audio clip")
    return [{"beat": m.beat_time, "sample_time": m.sample_time} for m in c.warp_markers]


def c_set_notes(cs, req):
    """Replace (or, with add=true, add to) the notes of a MIDI clip.
    notes: [{"pitch": 60, "start": 0.0, "duration": 0.5, "velocity": 100, "mute": false}, ...]"""
    _, _, c = _clip(cs, req)
    if not c.is_midi_clip:
        raise ValueError("not a MIDI clip")
    if not req.get("add"):
        c.remove_notes_extended(0, 128, 0.0, 1e6)
    specs = []
    for n in req.get("notes", []):
        specs.append(Live.Clip.MidiNoteSpecification(
            pitch=int(n["pitch"]), start_time=float(n["start"]), duration=float(n.get("duration", 0.25)),
            velocity=float(n.get("velocity", 100)), mute=bool(n.get("mute", False))))
    if specs:
        c.add_new_notes(tuple(specs))
    return {"notes": len(c.get_notes_extended(0, 128, 0.0, 1e6))}


# ------------------------------------------------------------------ parameters by target (mixer, chains, devices)
def _target_param(cs, req):
    """A parameter addressed by track + one of: device+param, mixer=volume|pan|send|activator|split_left|split_right|
    crossfader|cue|tempo (+index for send), chain (rack path 'D.C') + chain_mixer=volume|pan|send."""
    t = _track(cs, req["track"])
    if req.get("mixer"):
        md = t.mixer_device
        m = req["mixer"]
        table = {"volume": "volume", "pan": "panning", "activator": "track_activator", "split_left": "left_split_stereo",
                 "split_right": "right_split_stereo", "crossfader": "crossfader", "cue": "cue_volume", "tempo": "song_tempo"}
        if m == "send":
            return md.sends[int(req.get("index", 0))]
        return getattr(md, table[m])
    if req.get("chain") is not None:
        ch = _chain(cs, req["track"], req["chain"])
        m = req.get("chain_mixer", "volume")
        md = ch.mixer_device
        if m == "send":
            return md.sends[int(req.get("index", 0))]
        return {"volume": md.volume, "pan": md.panning}[m]
    _, _, d = _resolve(cs, req["track"], req["device"])
    return d.parameters[int(req["param"])]


def _chain(cs, ref, path):
    """'2.1' = chain 1 of the rack at device path 2 (paths can go deeper: '2.0.3.1')."""
    parts = str(path).split(".")
    _, _, rack = _resolve(cs, ref, ".".join(parts[:-1]))
    return rack.chains[int(parts[-1])]


def c_param_target(cs, req):
    p = _target_param(cs, req)
    info = _param_info(0, p)
    info.update({"automation_state": ["none", "playing", "overridden"][_safe(p, "automation_state", 0) or 0],
                 "default": _safe(p, "default_value"), "display_value": _safe(p, "display_value")})
    if req.get("set") is not None or req.get("text") is not None or req.get("value") is not None:
        info["change"] = _set_param(p, req)
    return info


def c_mixer_set(cs, req):
    """Mixer extras: panning_mode stereo|split, crossfade A|none|B, plus any mixer parameter via set_target."""
    t = _track(cs, req["track"])
    md = t.mixer_device
    if "panning_mode" in req:
        md.panning_mode = ["stereo", "split"].index(req["panning_mode"])
    if "crossfade" in req:
        md.crossfade_assign = ["A", "none", "B"].index(req["crossfade"])
    return {"track": t.name, "panning_mode": ["stereo", "split"][md.panning_mode],
            "crossfade": ["A", "none", "B"][md.crossfade_assign] if _safe(md, "crossfade_assign") is not None else None,
            "activator": str(md.track_activator) if _safe(md, "track_activator") is not None else None}


# ------------------------------------------------------------------ automation (clip envelopes)
def c_envelope(cs, req):
    """Read a clip envelope for a parameter: N points over the clip, or 'exists': false."""
    _, _, c = _clip(cs, req)
    p = _target_param(cs, dict(req, track=req.get("param_track", req["track"])))
    env = c.automation_envelope(p)
    if env is None:
        return {"exists": False, "param": p.name}
    n = int(req.get("points", 33))
    a, b = c.loop_start if c.looping else c.start_marker, c.loop_end if c.looping else c.end_marker
    pts = []
    for k in range(n):
        tm = a + (b - a) * k / (n - 1)
        # at the exact start of a step Live returns the previous value: read just after it
        v = env.value_at_time(min(b, tm + 1e-4))
        pts.append({"time": round(tm, 4), "value": v, "text": p.str_for_value(v)})
    return {"exists": True, "param": p.name, "points": pts}


def c_envelope_set(cs, req):
    """Write steps into a clip envelope: steps = [{"time": beats, "duration": beats, "value": raw}] (or "text").
    With clear=true the existing envelope for that parameter is removed first."""
    _, _, c = _clip(cs, req)
    p = _target_param(cs, dict(req, track=req.get("param_track", req["track"])))
    if req.get("clear"):
        c.clear_envelope(p)
    env = c.automation_envelope(p) or c.create_automation_envelope(p)
    for st in req.get("steps", []):
        v = st.get("value")
        if v is None:
            v = _value_for_text(p, st["text"])
        env.insert_step(float(st["time"]), float(st.get("duration", 0.0)), float(v))
    return {"param": p.name, "steps": len(req.get("steps", []))}


def c_envelope_clear(cs, req):
    _, _, c = _clip(cs, req)
    if req.get("all"):
        c.clear_all_envelopes()
        return {"cleared": "all"}
    p = _target_param(cs, dict(req, track=req.get("param_track", req["track"])))
    c.clear_envelope(p)
    return {"cleared": p.name}


def c_automation(cs, req):
    """Parameters of the set that are automated (playing) or overridden (automation switched off by a manual change)."""
    out = []
    for ref, t in _tracks(cs):
        md = t.mixer_device
        cand = [("mixer volume", md.volume), ("mixer pan", md.panning)] + [("send %s" % chr(65 + i), x) for i, x in enumerate(md.sends)]
        for d_path, d in _walk_devices(t):
            cand += [("%s / %s" % (d.name, p.name), p) for p in d.parameters]
        for label, p in cand:
            st = _safe(p, "automation_state", 0) or 0
            if st:
                out.append({"track": ref, "track_name": t.name, "param": label,
                            "state": ["none", "playing", "overridden"][st], "text": str(p)})
    return out


def c_re_enable_automation(cs, req):
    _song(cs).re_enable_automation()
    return True


def _walk_devices(cont, prefix=""):
    for j, d in enumerate(cont.devices):
        path = "%s%d" % (prefix, j)
        yield path, d
        if d.can_have_chains:
            for c, ch in enumerate(d.chains):
                for x in _walk_devices(ch, "%s.%d." % (path, c)):
                    yield x


# ------------------------------------------------------------------ racks
def c_rack(cs, req):
    """Chains of a rack (with their mixer), macros, variations; for a Drum Rack, the pads that hold chains."""
    _, _, d = _resolve(cs, req["track"], req["device"])
    if not d.can_have_chains:
        raise ValueError("%s is not a rack" % d.name)
    out = {"device": d.name, "class": d.class_name,
           "chains": [{"i": i, "name": ch.name, "mute": ch.mute, "solo": ch.solo, "color": _color(ch),
                       "volume": str(ch.mixer_device.volume) if _safe(ch, "mixer_device") else None,
                       "pan": str(ch.mixer_device.panning) if _safe(ch, "mixer_device") else None,
                       "devices": [x.name for x in ch.devices]} for i, ch in enumerate(d.chains)],
           "macros": [{"i": i, "name": p.name, "text": str(p)} for i, p in enumerate(d.parameters)
                      if p.name.startswith("Macro") or i in range(1, 1 + (_safe(d, "visible_macro_count", 0) or 0))],
           "variations": _safe(d, "variation_count", 0), "selected_variation": _safe(d, "selected_variation_index", -1)}
    if _safe(d, "can_have_drum_pads"):
        out["pads"] = [{"note": pad.note, "name": pad.name, "mute": pad.mute, "solo": pad.solo,
                        "chains": len(pad.chains)} for pad in d.drum_pads if len(pad.chains)]
    return out


def c_chain_set(cs, req):
    ch = _chain(cs, req["track"], req["chain"])
    if "name" in req:
        ch.name = req["name"]
    for k in ("mute", "solo"):
        if k in req:
            setattr(ch, k, bool(req[k]))
    if "color" in req:
        ch.color = int(str(req["color"]).lstrip("#"), 16)
    return {"chain": ch.name, "mute": ch.mute, "solo": ch.solo}


def c_chain_insert(cs, req):
    _, _, d = _resolve(cs, req["track"], req["device"])
    idx = int(req.get("index", len(d.chains)))
    d.insert_chain(idx)
    if req.get("name"):
        d.chains[idx].name = req["name"]
    return {"chains": [c.name for c in d.chains]}


def c_variation(cs, req):
    """Rack macro variations: store | recall <i> | delete <i> | randomize."""
    _, _, d = _resolve(cs, req["track"], req["device"])
    op = req["op"]
    if op == "store":
        d.store_variation()
    elif op == "recall":
        d.selected_variation_index = int(req["index"])
        d.recall_selected_variation()
    elif op == "delete":
        d.selected_variation_index = int(req["index"])
        d.delete_selected_variation()
    elif op == "randomize":
        d.randomize_macros()
    else:
        raise ValueError("op = store | recall | delete | randomize")
    return {"variations": d.variation_count, "selected": d.selected_variation_index}


def c_pad_set(cs, req):
    _, _, d = _resolve(cs, req["track"], req["device"])
    pad = d.drum_pads[int(req["note"])]
    for k in ("mute", "solo"):
        if k in req:
            setattr(pad, k, bool(req[k]))
    return {"note": pad.note, "name": pad.name, "mute": pad.mute, "solo": pad.solo}


# ------------------------------------------------------------------ plugin presets, device input routing (sidechain)
def c_presets(cs, req):
    """A plugin's own presets (if it publishes them to Live); with index, select one. Also all its parameter names."""
    _, _, d = _resolve(cs, req["track"], req["device"])
    presets = list(_safe(d, "presets", []) or [])
    if req.get("index") is not None:
        if not presets:
            raise ValueError("%s publishes no presets to Live" % d.name)
        d.selected_preset_index = int(req["index"])
    names = []
    try:
        names = list(d.get_parameter_names())
    except Exception:
        pass
    return {"device": d.name, "presets": presets, "selected": _safe(d, "selected_preset_index"),
            "all_parameter_names": names, "exposed": len(d.parameters)}


def c_device_routing(cs, req):
    """Input routing of a device that has one (sidechain of Compressor, Gate, Auto Filter...): show or set."""
    _, _, d = _resolve(cs, req["track"], req["device"])
    if not hasattr(d, "available_input_routing_types"):
        raise ValueError("%s has no input routing (sidechain)" % d.name)
    if req.get("type"):
        d.input_routing_type = _pick(d.available_input_routing_types, req["type"], "sidechain source")
    if req.get("channel"):
        d.input_routing_channel = _pick(d.available_input_routing_channels, req["channel"], "sidechain channel")
    return {"device": d.name, "type": _name(d, "input_routing_type"), "channel": _name(d, "input_routing_channel"),
            "types": [x.display_name for x in d.available_input_routing_types],
            "channels": [x.display_name for x in d.available_input_routing_channels]}


def c_warp_marker_move(cs, req):
    _, _, c = _clip(cs, req)
    c.move_warp_marker(float(req["beat"]), float(req["distance"]))
    return [{"beat": m.beat_time, "sample_time": m.sample_time} for m in c.warp_markers]


def c_groove(cs, req):
    """Grooves in the set's groove pool; with track+slot, show or set (index) or clear the clip's groove."""
    s = _song(cs)
    pool = list(_safe(_safe(s, "groove_pool"), "grooves", []) or [])
    out = {"pool": [{"i": i, "name": g.name} for i, g in enumerate(pool)]}
    if req.get("track") is not None:
        _, _, c = _clip(cs, req)
        if req.get("clear"):
            c.groove = None
        elif req.get("index") is not None:
            c.groove = pool[int(req["index"])]
        g = _safe(c, "groove")
        out["clip_groove"] = g.name if g is not None else None
    if req.get("amount") is not None:
        s.groove_amount = float(req["amount"])
    out["amount"] = _safe(s, "groove_amount")
    return out


def c_arr_from_session(cs, req):
    """Lay session scenes out in the arrangement: scenes = [[scene index, bars], ...], starting at `time` beats."""
    s = _song(cs)
    t0 = float(req.get("time", 0.0))
    bpb = _beats_per_bar(s)
    at = t0
    placed = 0
    for sc_i, bars in req["scenes"]:
        for t in s.tracks:
            slot = t.clip_slots[int(sc_i)]
            if slot.has_clip:
                t.duplicate_clip_to_arrangement(slot.clip, at)
                placed += 1
        at += float(bars) * bpb
    return {"clips": placed, "end": at, "end_bar": _bar(s, at)}


# ------------------------------------------------------------------ native devices without the browser
def c_insert_device(cs, req):
    """Insert a Live device by name ('EQ Eight', 'Compressor', 'Utility'...) at an index, on a track or in a chain."""
    cont = _chain(cs, req["track"], req["chain"]) if req.get("chain") is not None else _track(cs, req["track"])
    idx = int(req.get("index", len(cont.devices)))
    cont.insert_device(req["name"], idx)
    return {"devices": [d.name for d in cont.devices]}


def c_duplicate_device(cs, req):
    cont, i, d = _resolve(cs, req["track"], req["device"])
    cont.duplicate_device(i)
    return {"devices": [x.name for x in cont.devices]}


# ------------------------------------------------------------------ arrangement editing
def c_arr_add(cs, req):
    """Put a clip in the arrangement: from a session slot (copy), a new empty MIDI clip, or an audio file."""
    t = _track(cs, req["track"])
    at = float(req["time"])
    if req.get("slot") is not None:
        t.duplicate_clip_to_arrangement(t.clip_slots[int(req["slot"])].clip, at)
    elif req.get("file"):
        t.create_audio_clip(req["file"], at)
    else:
        t.create_midi_clip(at, float(req.get("length", 4.0)))
    return c_arrangement(cs, {"track": req["track"]})


# ------------------------------------------------------------------ view
def c_show(cs, req):
    """Bring something into view so the user sees what changed: a track, a device, a clip (session or arrangement)."""
    s = _song(cs)
    app_view = Live.Application.get_application().view
    if req.get("track") is not None:
        t = _track(cs, req["track"])
        s.view.selected_track = t
        if req.get("device") is not None:
            _, _, d = _resolve(cs, req["track"], req["device"])
            s.view.select_device(d)
            app_view.show_view("Detail/DeviceChain")
        elif req.get("slot") is not None:
            s.view.highlighted_clip_slot = t.clip_slots[int(req["slot"])]
            app_view.show_view("Detail/Clip")
    if req.get("view"):
        app_view.show_view(req["view"])
        app_view.focus_view(req["view"])
    return True


# ------------------------------------------------------------------ snapshot of every value (for diff / restore)
def _snap_param(out, key, label, p):
    try:
        out.append([key, label, p.value, str(p)])
    except Exception:
        pass


def c_snapshot(cs, req):
    """Every mixer, device and chain parameter of the set, plus track mute/solo/arm: [[key, label, raw, text]].
    Keys are stable addresses usable with set_values."""
    out = []
    for ref, t in _tracks(cs):
        md = t.mixer_device
        for nm in ("volume", "panning", "track_activator"):
            p = _safe(md, nm)
            if p is not None:
                _snap_param(out, "%s|mix|%s" % (ref, nm), "%s / mixer %s" % (t.name, nm), p)
        for i, x in enumerate(md.sends):
            _snap_param(out, "%s|mix|send|%d" % (ref, i), "%s / send %s" % (t.name, chr(65 + i)), x)
        for k in ("mute", "solo", "arm"):
            v = _safe(t, k)
            if v is not None and (k != "arm" or _safe(t, "can_be_armed")):
                out.append(["%s|track|%s" % (ref, k), "%s / %s" % (t.name, k), bool(v), "on" if v else "off"])
        for path, d in _walk_devices(t):
            for i, p in enumerate(d.parameters):
                _snap_param(out, "%s|dev|%s|%d|%s" % (ref, path, i, d.name), "%s / %s / %s" % (t.name, d.name, p.name), p)
            if d.can_have_chains:
                for c, ch in enumerate(d.chains):
                    cmd = _safe(ch, "mixer_device")
                    if cmd is not None:
                        _snap_param(out, "%s|chain|%s.%d|volume" % (ref, path, c), "%s / %s / %s volume" % (t.name, d.name, ch.name), cmd.volume)
                        _snap_param(out, "%s|chain|%s.%d|panning" % (ref, path, c), "%s / %s / %s pan" % (t.name, d.name, ch.name), cmd.panning)
    s = _song(cs)
    out.append(["song|tempo", "song tempo", s.tempo, "%.2f" % s.tempo])
    return out


def c_set_values(cs, req):
    """Set many values by snapshot key: items = [[key, raw], ...]. Device keys carry the device name and are skipped
    when the device at that path changed. Returns what was set and what was skipped."""
    s = _song(cs)
    done, skipped = 0, []
    for key, v in req["items"]:
        try:
            parts = key.split("|")
            if parts[0] == "song" and parts[1] == "tempo":
                s.tempo = float(v)
            elif parts[1] == "mix":
                md = _track(cs, parts[0]).mixer_device
                p = md.sends[int(parts[3])] if parts[2] == "send" else getattr(md, parts[2])
                p.value = float(v)
            elif parts[1] == "track":
                setattr(_track(cs, parts[0]), parts[2], bool(v))
            elif parts[1] == "dev":
                _, _, d = _resolve(cs, parts[0], parts[2])
                if d.name != parts[4]:
                    skipped.append([key, "device is now %s" % d.name])
                    continue
                d.parameters[int(parts[3])].value = float(v)
            elif parts[1] == "chain":
                ch = _chain(cs, parts[0], parts[2])
                getattr(ch.mixer_device, parts[3]).value = float(v)
            else:
                skipped.append([key, "unknown key"])
                continue
            done += 1
        except Exception as e:
            skipped.append([key, "%s: %s" % (type(e).__name__, e)])
    return {"set": done, "skipped": skipped}


# ------------------------------------------------------------------ events pushed by Live (listeners)
def _ev_state(cs):
    if not hasattr(cs, "_lc_ev"):
        cs._lc_ev = {"listeners": [], "events": [], "seq": 0}
    return cs._lc_ev


def _ev_push(cs, key, label, value):
    st = _ev_state(cs)
    st["seq"] += 1
    st["events"].append({"seq": st["seq"], "key": key, "label": label, "value": value})
    if len(st["events"]) > 2000:
        del st["events"][:1000]


def _ev_add(cs, obj, prop, fn):
    add = getattr(obj, "add_%s_listener" % prop, None)
    rem = getattr(obj, "remove_%s_listener" % prop, None)
    if add is None or rem is None:
        return
    try:
        add(fn)
        _ev_state(cs)["listeners"].append((rem, fn))
    except Exception:
        pass


def c_events_stop(cs, req):
    st = _ev_state(cs)
    for rem, fn in st["listeners"]:
        try:
            rem(fn)
        except Exception:
            pass
    st["listeners"] = []
    return True


def c_events_start(cs, req):
    """Register listeners on the song, tracks, mixers and every device parameter. Events are read with events_poll."""
    c_events_stop(cs, req)
    s = _song(cs)
    for prop in ("tempo", "is_playing", "record_mode", "loop", "metronome", "signature_numerator", "tracks", "scenes"):
        _ev_add(cs, s, prop, lambda prop=prop: _ev_push(cs, "song|" + prop, "song " + prop,
                                                       str(len(getattr(s, prop))) if prop in ("tracks", "scenes") else str(getattr(s, prop))))
    for ref, t in _tracks(cs):
        for prop in ("mute", "solo", "arm", "name", "devices"):
            if prop == "arm" and not _safe(t, "can_be_armed"):
                continue
            if ref == "m" and prop in ("mute", "solo", "arm"):
                continue
            _ev_add(cs, t, prop, lambda t=t, ref=ref, prop=prop: _ev_push(
                cs, "%s|track|%s" % (ref, prop), "%s / %s" % (t.name, prop),
                str(len(t.devices)) + " devices" if prop == "devices" else str(getattr(t, prop))))
        md = t.mixer_device
        params = [("mixer volume", md.volume), ("mixer pan", md.panning)] + [("send %s" % chr(65 + i), x) for i, x in enumerate(md.sends)]
        for path, d in _walk_devices(t):
            params += [("%s / %s" % (d.name, p.name), p) for p in d.parameters]
        for label, p in params:
            _ev_add(cs, p, "value", lambda p=p, t=t, ref=ref, label=label: _ev_push(
                cs, "%s|param|%s" % (ref, label), "%s / %s" % (t.name, label), str(p)))
    return {"listeners": len(_ev_state(cs)["listeners"]), "seq": _ev_state(cs)["seq"]}


def c_events_poll(cs, req):
    st = _ev_state(cs)
    since = int(req.get("since", 0))
    return {"seq": st["seq"], "events": [e for e in st["events"] if e["seq"] > since], "listening": len(st["listeners"])}


# ------------------------------------------------------------------ audio capture (optional)
def c_capture(cs, req):
    """Record from a CoreAudio input device with ffmpeg launched by Live itself, so it inherits Live's
    microphone permission (a terminal or a tmux session usually doesn't have it). Off by default:
    enable it with {"capture": {"enabled": true, "device": "<device name>"}} in ~/.config/live-cli/config.json.
    Returns immediately; the file is complete after `seconds`."""
    import subprocess
    import threading
    path = os.path.expanduser("~/.config/live-cli/config.json")
    cfg = _config().get("capture", {})
    if not cfg.get("enabled"):
        if os.path.exists(path) and not os.access(path, os.R_OK):
            raise ValueError("Live cannot read %s (permissions)" % path)
        raise ValueError("capture is disabled in %s (see README: capture)" % path)
    dev = req.get("device") or cfg.get("device")
    if not dev:
        raise ValueError("no capture device configured")
    sec = min(float(req.get("seconds", 10)), float(cfg.get("max_seconds", 600)))
    out = req["file"]
    ffmpeg = cfg.get("ffmpeg", "/opt/homebrew/bin/ffmpeg")
    cmd = ["/usr/bin/nice", "-n", str(cfg.get("nice", 15)), ffmpeg, "-hide_banner", "-v", "error",
           "-f", "avfoundation", "-i", ":" + dev, "-t", str(sec), "-c:a", "pcm_f32le", "-y", out]
    p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=open(out + ".log", "w"), start_new_session=True)
    threading.Thread(target=p.wait, daemon=True).start()  # reap the process (no zombies)
    return {"pid": p.pid, "file": out, "seconds": sec}
