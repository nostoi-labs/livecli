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

VERSION = 10
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
    if re.search(r"\d\s*k(hz)?\b", t):
        v *= 1000
    return v


def _set_by_text(p, text):
    """Set a parameter from its display text ('-3 dB', '250 Hz', 'Bell'). Works for Live's own devices
    (str_for_value). Plugins don't translate values in advance: the CLI sets them by read-back."""
    if p.is_quantized:
        choices = [p.str_for_value(v) for v in range(int(p.min), int(p.max) + 1)]
        for k, c in enumerate(choices):
            if str(text).strip().lower() == c.strip().lower():
                p.value = int(p.min) + k
                return
        raise ValueError("'%s' is not one of: %s" % (text, ", ".join(choices)))
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
    p.value = min(cands, key=lambda x: (abs((_num(p.str_for_value(x)) or 0) - goal), abs(x - (lo + hi) / 2.0)))


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


def _clip_info(c):
    e = {"name": c.name, "color": _color(c), "length": c.length, "looping": c.looping,
         "loop_start": c.loop_start, "loop_end": c.loop_end, "midi": c.is_midi_clip,
         "playing": c.is_playing, "triggered": c.is_triggered, "muted": c.muted}
    if c.is_audio_clip:
        e.update({"file": getattr(c, "file_path", None), "warping": c.warping,
                  "gain": getattr(c, "gain_display_string", None)})
    return e


def _clip(cs, req):
    t = _track(cs, req["track"])
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
             "pan": str(md.panning),
             "devices": [_dev_info(d, str(j)) for j, d in enumerate(t.devices)]}
        if ref != "m":
            e.update({"mute": t.mute, "solo": t.solo, "output": _name(t, "output_routing_type"),
                      "output_channel": _name(t, "output_routing_channel")})
        if ref[0] == "t":
            e.update({"kind": "group" if t.is_foldable else ("midi" if t.has_midi_input else "audio"),
                      "arm": t.arm if t.can_be_armed else None,
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
        for c in getattr(t, "arrangement_clips", []):
            out.append({"track": ref, "track_name": t.name, "name": c.name, "start": c.start_time,
                        "end": c.end_time, "start_bar": _bar(s, c.start_time), "end_bar": _bar(s, c.end_time),
                        "muted": c.muted, "midi": c.is_midi_clip,
                        "file": getattr(c, "file_path", None) if c.is_audio_clip else None})
    return out


def c_notes(cs, req):
    _, _, c = _clip(cs, req)
    if not c.is_midi_clip:
        raise ValueError("not a MIDI clip")
    notes = c.get_notes_extended(0, 128, 0.0, 1e6)
    return [{"pitch": n.pitch, "start": n.start_time, "duration": n.duration, "velocity": n.velocity,
             "mute": n.mute, "probability": n.probability} for n in notes]


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
    _, slot, c = _clip(cs, req)
    name = c.name
    slot.delete_clip()
    return {"deleted": name}


def c_clip_duplicate(cs, req):
    t = _track(cs, req["track"])
    i = t.duplicate_clip_slot(int(req["slot"]))
    return {"slot": i}


def c_clip_set(cs, req):
    _, _, c = _clip(cs, req)
    if "name" in req:
        c.name = req["name"]
    if "color" in req:
        c.color = int(str(req["color"]).lstrip("#"), 16)
    for k in ("looping", "muted"):
        if k in req:
            setattr(c, k, bool(req[k]))
    for k in ("loop_start", "loop_end"):
        if k in req:
            setattr(c, k, float(req[k]))
    return _clip_info(c)


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
