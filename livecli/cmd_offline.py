"""Offline work on Live Set files (.als): lint and diff. Read-only: never writes a set."""
import gzip
import os
import re
import xml.etree.ElementTree as ET

from . import cli as C
from .client import LiveError

FRIENDLY = {"Eq8": "EQ Eight", "GlueCompressor": "Glue Compressor", "ProxyAudioEffectDevice": "External Audio Effect",
            "ProxyInstrumentDevice": "External Instrument", "AudioEffectGroupDevice": "Audio Effect Rack",
            "InstrumentGroupDevice": "Instrument Rack", "DrumGroupDevice": "Drum Rack", "MidiEffectGroupDevice": "MIDI Effect Rack",
            "Compressor2": "Compressor", "StereoGain": "Utility", "AutoFilter": "Auto Filter", "MultibandDynamics": "Multiband Dynamics",
            "OriginalSimpler": "Simpler", "MultiSampler": "Sampler", "InstrumentVector": "Wavetable"}


def _v(el, path, default=None):
    x = el.find(path) if el is not None else None
    return x.get("Value") if x is not None and x.get("Value") is not None else default


def _routing(el):
    if el is None:
        return None, "", ""
    return _v(el, ".//Target"), _v(el, ".//UpperDisplayString", ""), _v(el, ".//LowerDisplayString", "")


def _channels(lower):
    m = re.match(r"\s*(\d+)(?:/(\d+))?", lower or "")
    if not m:
        return []
    a = int(m.group(1))
    b = int(m.group(2)) if m.group(2) else a
    return list(range(a, b + 1))


def _devname(d):
    un = _v(d, "UserName")
    if d.tag in ("PluginDevice", "AuPluginDevice"):
        return un or _v(d, ".//PluginDesc//PlugName") or _v(d, ".//PluginDesc//Name") or d.tag
    preset = _v(d, "LastPresetRef//RelativePath") or _v(d, "LastPresetRef//Path")
    base = FRIENDLY.get(d.tag, d.tag)
    if un:
        return un
    if preset:
        pn = os.path.splitext(os.path.basename(preset))[0]
        return base if pn == base else "%s (%s)" % (pn, base)
    return base


def _devices(chain):
    out = []
    if chain is None:
        return out
    for d in list(chain):
        e = {"tag": d.tag, "name": _devname(d), "on": _v(d, "On/Manual", "true") == "true"}
        if d.tag == "ProxyAudioEffectDevice":
            _, ou, ol = _routing(d.find("OutputHelper"))
            _, iu, il = _routing(d.find("InputHelper"))
            e["ext_out"] = _channels(ol) if ou and ou != "No Output" else []
            e["ext_in"] = _channels(il) if iu and iu != "No Input" else []
        inner = []
        for br in d.findall(".//Branches"):
            for b in list(br):
                inner += _devices(b.find(".//DeviceChain/Devices"))
        if inner:
            e["inside"] = inner
        out.append(e)
    return out


def parse_als(path):
    try:
        root = ET.fromstring(gzip.open(path).read())
    except (OSError, ET.ParseError) as e:
        raise LiveError("cannot read %s: %s" % (path, e))
    ls = root.find("LiveSet")
    main = ls.find("MainTrack")
    if main is None:
        main = ls.find("MasterTrack")
    res = {"file": os.path.abspath(path), "creator": root.get("Creator"),
           "tempo": float(_v(main, ".//Tempo/Manual", "0") or 0) if main is not None else None, "tracks": []}
    nt = nr = 0
    for t in list(ls.find("Tracks")):
        kind = t.tag.replace("Track", "").lower()
        ref = "r%d" % nr if t.tag == "ReturnTrack" else "t%d" % nt
        if t.tag == "ReturnTrack":
            nr += 1
        else:
            nt += 1
        _, ou, ol = _routing(t.find("DeviceChain/AudioOutputRouting"))
        samples = sorted(set(x.get("Value") for x in t.iter("Path") if x.get("Value") and
                             re.search(r"\.(wav|aif|aiff|flac|mp3|m4a|ogg)$", x.get("Value"), re.I)))
        res["tracks"].append({
            "ref": ref, "kind": kind, "name": _v(t, "Name/EffectiveName", ""),
            "output": ou, "output_channel": ol, "ext_out": _channels(ol) if ou == "Ext. Out" else [],
            "muted": _v(t, ".//Speaker/Manual", "true") == "false",
            "frozen": _v(t, "Freeze", "false") == "true",
            "devices": _devices(t.find("DeviceChain/DeviceChain/Devices")),
            "session_clips": len(t.findall(".//ClipSlot//AudioClip")) + len(t.findall(".//ClipSlot//MidiClip")),
            "arrangement_clips": len(t.findall(".//ArrangerAutomation/Events/AudioClip")) +
                                 len(t.findall(".//ArrangerAutomation/Events/MidiClip")),
            "samples": samples})
    res["master_devices"] = _devices(main.find("DeviceChain/DeviceChain/Devices")) if main is not None else []
    return res


def _walk(devs, path=""):
    for k, d in enumerate(devs):
        yield "%s%d" % (path, k), d
        for x in _walk(d.get("inside", []), "%s%d." % (path, k)):
            yield x


def lint(res):
    issues = []
    names = {}
    for t in res["tracks"]:
        names.setdefault(t["name"], []).append(t["ref"])
        if t["kind"] in ("audio", "midi") and not t["devices"] and t["session_clips"] + t["arrangement_clips"] == 0:
            issues.append(("info", t["ref"], t["name"], "empty track (no clips, no devices)"))
        if t["muted"]:
            issues.append(("info", t["ref"], t["name"], "track is off (activator)"))
        if t["frozen"]:
            issues.append(("info", t["ref"], t["name"], "frozen"))
        if re.fullmatch(r"\d+-(Audio|MIDI)", t["name"]):
            issues.append(("info", t["ref"], t["name"], "default name"))
        for s in t["samples"]:
            if not os.path.exists(s):
                issues.append(("warning", t["ref"], t["name"], "missing sample: %s" % s))
        for p, d in _walk(t["devices"]):
            if not d["on"]:
                issues.append(("info", t["ref"], t["name"], "device off: %s [%s]" % (d["name"], p)))
    for p, d in _walk(res["master_devices"]):
        if not d["on"]:
            issues.append(("info", "m", "Main", "device off: %s [%s]" % (d["name"], p)))
    for n, refs in names.items():
        if len(refs) > 1:
            issues.append(("info", ",".join(refs), n, "duplicate track name"))
    # hardware I/O: two active External Audio Effects (or Ext. Out track outputs) on the same output channel
    users = {}
    for t in res["tracks"] + [{"ref": "m", "name": "Main", "devices": res["master_devices"], "ext_out": []}]:
        for ch in t.get("ext_out", []):
            users.setdefault(("out", ch), []).append("%s %s (track output)" % (t["ref"], t["name"]))
        for p, d in _walk(t["devices"]):
            if d["tag"] == "ProxyAudioEffectDevice" and d["on"]:
                for ch in d.get("ext_out", []):
                    users.setdefault(("out", ch), []).append("%s %s / %s" % (t["ref"], t["name"], d["name"]))
                for ch in d.get("ext_in", []):
                    users.setdefault(("in", ch), []).append("%s %s / %s" % (t["ref"], t["name"], d["name"]))
    for (side, ch), who in sorted(users.items()):
        ext = [w for w in who if "(track output)" not in w]
        if len(ext) > 1 or (side == "out" and len(ext) >= 1 and len(who) > len(ext)):
            issues.append(("warning", "-", "hardware %s %d" % (side, ch), "shared by: %s" % "; ".join(who)))
    return issues


@C.command("als-lint", "als-lint <file.als>", "check a set without opening it: missing samples, hardware outputs/inputs "
           "shared by several External Audio Effects (feedback risk), devices off, empty or default-named tracks", "other")
def c_als_lint(a):
    C.need(a, 1, "als-lint <file.als>")
    res = parse_als(a[0])
    issues = lint(res)
    if C.JSON:
        return C.out({"file": res["file"], "issues": [dict(zip(("level", "track", "name", "issue"), i)) for i in issues]})
    print("%s: %d warnings, %d notes" % (res["file"], sum(1 for i in issues if i[0] == "warning"),
                                         sum(1 for i in issues if i[0] == "info")))
    for lvl, ref, name, msg in sorted(issues, key=lambda i: (i[0] != "warning", i[1])):
        print("  %-7s %-6s %-26s %s" % (lvl, ref, name[:26], msg))


def diff(a, b):
    out = []
    if a["tempo"] != b["tempo"]:
        out.append(("tempo", "", "%s -> %s" % (a["tempo"], b["tempo"])))
    an = {t["name"]: t for t in a["tracks"]}
    bn = {t["name"]: t for t in b["tracks"]}
    for n in an:
        if n not in bn:
            out.append(("track removed", n, an[n]["ref"]))
    for n in bn:
        if n not in an:
            out.append(("track added", n, bn[n]["ref"]))
    for n in an:
        if n in bn:
            ta, tb = an[n], bn[n]
            da = [d["name"] for _, d in _walk(ta["devices"])]
            db = [d["name"] for _, d in _walk(tb["devices"])]
            if da != db:
                rem = [x for x in da if x not in db]
                add = [x for x in db if x not in da]
                out.append(("devices", n, "%s%s%s" % ("- " + ", ".join(rem) if rem else "", "  " if rem and add else "",
                                                     "+ " + ", ".join(add) if add else "reordered")))
            oa = [d["on"] for _, d in _walk(ta["devices"])]
            ob = [d["on"] for _, d in _walk(tb["devices"])]
            if da == db and oa != ob:
                ch = ["%s %s" % (nm, "on" if y else "off") for nm, x, y in zip(da, oa, ob) if x != y]
                out.append(("device on/off", n, ", ".join(ch)))
            for k in ("output", "output_channel", "muted", "frozen"):
                if ta[k] != tb[k]:
                    out.append((k, n, "%s -> %s" % (ta[k], tb[k])))
            for k in ("session_clips", "arrangement_clips"):
                if ta[k] != tb[k]:
                    out.append((k.replace("_", " "), n, "%d -> %d" % (ta[k], tb[k])))
    ma = [d["name"] for d in a["master_devices"]]
    mb = [d["name"] for d in b["master_devices"]]
    if ma != mb:
        out.append(("master devices", "Main", "%s -> %s" % (", ".join(ma), ", ".join(mb))))
    return out


@C.command("als-diff", "als-diff <old.als> <new.als>", "what changed between two versions of a set: tracks, devices, "
           "on/off, routing, clips, tempo", "other")
def c_als_diff(a):
    C.need(a, 2, "als-diff <old.als> <new.als>")
    d = diff(parse_als(a[0]), parse_als(a[1]))
    if C.JSON:
        return C.out([dict(zip(("what", "track", "change"), x)) for x in d])
    print("%d differences" % len(d))
    for what, n, ch in d:
        print("  %-18s %-26s %s" % (what, n[:26], ch))
