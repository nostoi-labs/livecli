"""Hybrid studio: fingerprint of a device or hardware insert (response, noise, distortion, latency), and session recall
(Live snapshot + outboard settings from SessionRecall + fingerprints)."""
import glob
import json
import math
import os
import sqlite3
import statistics
import time

from . import cli as C
from . import probes as P
from .client import CONFIG_DIR, LiveError, call, config
from .cmd_mix import ensure_probe, remove_added

FP_DIR = os.path.join(CONFIG_DIR, "fingerprints")
RECALL_DIR = os.path.join(CONFIG_DIR, "recalls")


def _signal(src, **kw):
    return src[0].request(dict({"cmd": "signal", "uid": src[1]["uid"]}, **kw), timeout=10)


def _band_index(f):
    return min(range(len(P.CENTERS)), key=lambda k: abs(math.log(P.CENTERS[k] / f)))


def fingerprint(ref, dev_path, level_db=-20.0, seconds=6.0):
    """gen probe -> ref probe -> DEVICE -> det probe. gen emits the test signals, ref sees them, det sees the result."""
    gen, a1 = ensure_probe(ref, ("before", dev_path), "fp-gen")
    dev_now = str(int(dev_path) + 1) if a1 else dev_path
    refp, a2 = ensure_probe(ref, ("before", dev_now), "fp-ref")
    if a2:
        dev_now = str(int(dev_now) + 1)
    det, a3 = ensure_probe(ref, ("after", dev_now), "fp-det")
    labels = [x for x, ad in (("fp-gen", a1), ("fp-ref", a2), ("fp-det", a3)) if ad]
    if not gen[0].has("signal"):
        remove_added({ref: labels})
        raise LiveError("the probes don't support 'signal' (Reference/Loudline Probe 0.1.3+)", code="probe")
    vol = call("mixer", track=ref)["volume"]
    out = {"level_db": level_db}
    try:
        call("set_mixer", track=ref, what="volume", value=0.0)       # silent while measuring
        # 1. noise floor: silence into the device
        _signal(gen, kind="silence", seconds=4.0, mode="replace")
        time.sleep(0.8)
        out["noise"] = P.collect([det], 2.5)[det[1]["uid"]]
        # 2. response, gain, latency: pink noise
        _signal(gen, kind="pink", seconds=seconds + 2, level_db=level_db, mode="replace", seed=7)
        time.sleep(0.8)
        m = P.collect([refp, det], seconds - 1)
        mr, md = m[refp[1]["uid"]], m[det[1]["uid"]]
        out["response_db"] = [None if x <= -90 or y <= -110 else round(y - x, 2) for x, y in zip(mr["bands_mid"], md["bands_mid"])]
        out["gain_db"] = round(md["rms_db"] - mr["rms_db"], 2)
        if refp[0].has("delay"):
            try:
                d = refp[0].request({"cmd": "delay", "a": refp[1]["uid"], "b": det[1]["uid"], "seconds": 1.5, "max_lag_ms": 200},
                                    timeout=10)
                out["latency_samples"], out["latency_ms"] = d["delay_samples"], d["delay_ms"]
                out["polarity"], out["correlation"] = d.get("polarity"), d.get("correlation_peak")
            except LiveError:
                pass
        time.sleep(0.5)
        # 3. distortion: 1 kHz sine, measured by the probe after the device (capability 'thd', PROTOCOL 5.13).
        #    1/3-octave bands are too coarse for THD (their own leakage is ~12 %), so without 'thd' it is not reported.
        if det[0].has("thd"):
            _signal(gen, kind="sine", freq=1000, seconds=4.0, level_db=level_db, mode="replace")
            time.sleep(0.8)
            r = det[0].request({"cmd": "thd", "uid": det[1]["uid"], "freq": 1000, "seconds": 2.0}, timeout=10)
            out["thd_n_pct"], out["thd_n_db"] = r.get("thd_n_pct"), r.get("thd_n_db")
            out["harmonics_db"] = r.get("harmonics_db")
        else:
            out["thd_n_pct"] = out["thd_n_db"] = None
        out["noise_rms_db"] = out["noise"].get("rms_db")
    finally:
        _signal(gen, stop=True) if gen[0].has("signal") else None
        call("set_mixer", track=ref, what="volume", text=vol)
        remove_added({ref: labels})
    return out


@C.command("fingerprint", "fingerprint <track> <device> [name] [--level -20] [--compare]", "measure a device or a "
           "hardware insert with test signals from the probes: frequency response, gain, noise floor, THD+N, latency. "
           "Saved under a name (default: the device name); --compare shows what changed since the last one. The track "
           "is silenced while measuring, signals at -20 dBFS", "audio")
def c_fingerprint(a):
    C.need(a, 2, "fingerprint <track> <device> [name] [--level -20] [--compare]")
    t = C.track(a[0])
    d = C.find_device(t, a[1])
    if "." in d["path"]:
        raise LiveError("fingerprint works on top-level devices")
    level = float(a[a.index("--level") + 1]) if "--level" in a else -20.0
    pos = [x for k, x in enumerate(a[2:], 2) if not x.startswith("--") and a[k - 1] != "--level"]
    name = pos[0] if pos else d["name"]
    fp = fingerprint(t["ref"], d["path"], level)
    fp.update({"name": name, "track": t["name"], "device": d["name"], "time": time.strftime("%Y-%m-%d %H:%M:%S")})
    os.makedirs(FP_DIR, exist_ok=True)
    prev = sorted(glob.glob(os.path.join(FP_DIR, "%s--*.json" % _safe_name(name))))
    path = os.path.join(FP_DIR, "%s--%s.json" % (_safe_name(name), time.strftime("%Y%m%d-%H%M%S")))
    with open(path, "w") as fh:
        json.dump(fp, fh)
    cmp_ = compare_fp(json.load(open(prev[-1])), fp) if prev else None
    if C.JSON:
        return C.out({"fingerprint": fp, "file": path, "changes": cmp_})
    resp = [x for x in fp["response_db"] if x is not None]
    base = statistics.median(resp) if resp else 0
    regions = P.regions(P.CENTERS, fp["response_db"], base, threshold=0.5)
    print("%s (%s / %s) at %g dBFS:" % (name, t["name"], d["name"], level))
    print("  gain %+.2f dB, latency %s, polarity %s" % (fp["gain_db"], "%.2f ms (%d samples)" % (fp["latency_ms"], fp["latency_samples"])
          if "latency_ms" in fp else "?", {1: "normal", -1: "inverted"}.get(fp.get("polarity"), "?")))
    print("  noise floor %.1f dBFS RMS, THD+N %s" % (
        fp["noise_rms_db"] or -150, "%.3f %%" % fp["thd_n_pct"] if fp.get("thd_n_pct") is not None else
        "n/a (needs probes with 'thd')"))
    hs = fp.get("harmonics_db") or []
    if any(h is not None for h in hs):
        print("  harmonics (dBc): " + ", ".join("H%d %.0f" % (k + 2, h) for k, h in enumerate(hs[:5]) if h is not None))
    print("  response: " + ("flat within 0.5 dB" if not regions else
          "; ".join("%+.1f dB at %s Hz" % (r["db"], P.fmt_hz(r["at"])) for r in regions)))
    if cmp_ is not None:
        print("  since %s: %s" % (cmp_["since"], "no relevant change" if not cmp_["changes"] else "; ".join(cmp_["changes"])))
    print("  saved: %s" % path)


def _safe_name(n):
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in n)


def compare_fp(old, new):
    ch = []
    if abs((new.get("gain_db") or 0) - (old.get("gain_db") or 0)) >= 0.5:
        ch.append("gain %+.1f dB" % (new["gain_db"] - old["gain_db"]))
    if "latency_samples" in new and "latency_samples" in old and abs(new["latency_samples"] - old["latency_samples"]) > 2:
        ch.append("latency %+d samples" % (new["latency_samples"] - old["latency_samples"]))
    if (new.get("noise_rms_db") or -150) - (old.get("noise_rms_db") or -150) >= 3:
        ch.append("noise floor %+.1f dB" % (new["noise_rms_db"] - old["noise_rms_db"]))
    if new.get("thd_n_db") is not None and old.get("thd_n_db") is not None and new["thd_n_db"] - old["thd_n_db"] >= 3:
        ch.append("THD+N %+.1f dB" % (new["thd_n_db"] - old["thd_n_db"]))
    diffs = [None if x is None or y is None else y - x for x, y in zip(old["response_db"], new["response_db"])]
    valid = [x for x in diffs if x is not None]
    base = statistics.median(valid) if valid else 0
    for r in P.regions(P.CENTERS, diffs, base, threshold=1.0):
        ch.append("response %+.1f dB at %s Hz" % (r["db"], P.fmt_hz(r["at"])))
    if new.get("polarity") and old.get("polarity") and new["polarity"] != old["polarity"]:
        ch.append("polarity changed")
    return {"since": old.get("time"), "changes": ch}


@C.command("fingerprints", "fingerprints [name]", "saved fingerprints (all, or the history of one)", "audio")
def c_fingerprints(a):
    files = sorted(glob.glob(os.path.join(FP_DIR, "*.json")))
    rows = []
    for f in files:
        x = json.load(open(f))
        if a and x.get("name") != a[0]:
            continue
        rows.append({"name": x.get("name"), "time": x.get("time"), "gain_db": x.get("gain_db"), "latency_ms": x.get("latency_ms"),
                     "noise_rms_db": x.get("noise_rms_db"), "thd_n_pct": x.get("thd_n_pct"), "file": f})
    C.out(rows, "\n".join("  %-24s %s  gain %+.2f  latency %s ms  noise %.1f  THD+N %s %%" % (
        r["name"][:24], r["time"], r["gain_db"] or 0, r["latency_ms"], r["noise_rms_db"] or -150, r["thd_n_pct"]) for r in rows)
        or "no fingerprints")


# ------------------------------------------------------------------ SessionRecall (read-only)
def _sr_db():
    p = os.path.expanduser(config().get("sessionrecall_db", "~/Documents/SessionRecall/SessionRecall.db"))
    if not os.path.exists(p):
        raise LiveError("SessionRecall database not found: %s (config 'sessionrecall_db')" % p, code="not_found")
    return sqlite3.connect("file:%s?mode=ro" % p, uri=True)


def _clock(angle):
    """SessionRecall knob angle (30 = fully left, 180 = 12 o'clock, 330 = fully right) as a clock position."""
    h = ((angle - 180) / 30.0 + 12) % 12
    hh = int(h) or 12
    mm = int(round((h - int(h)) * 60))
    if mm == 60:
        hh, mm = (hh % 12) + 1, 0
    return "%d:%02d" % (hh, mm)


def sr_sessions(db):
    return [{"id": r[0], "title": r[1], "created": r[2], "version": r[3], "parent": r[4]} for r in
            db.execute("select id, Title, Created, Version, parent_id from user_sessions order by id")]


def sr_find(db, q):
    ss = sr_sessions(db)
    if str(q).isdigit():
        hit = [s for s in ss if s["id"] == int(q)]
    else:
        hit = [s for s in ss if str(q).lower() in (s["title"] or "").lower()]
    if len(hit) != 1:
        raise LiveError("SessionRecall session '%s': %s" % (q, "not found" if not hit else
                        "ambiguous: " + ", ".join("%d %s" % (s["id"], s["title"]) for s in hit)))
    return hit[0]


def sr_settings(db, sid, version=None):
    """Outboard settings of a SessionRecall session: every version is a complete photo, take the latest (or `version`)."""
    labels = config().get("sessionrecall_labels", {})
    if version is None:
        version = db.execute("select Version from user_sessions where id = ?", (sid,)).fetchone()[0]
    rows = db.execute("select s.id, s.version, s.ordering, s.Channel1Values, s.Channel2Values, s.DeviceUserTitle, s.UserNotes, "
                      "t.BrandName, t.DeviceName, t.id, s.LinkedChannels, s.DeviceChannelCount from user_devices_settings s "
                      "join device_templates t on t.id = s.id_template where s.id_session = ? and s.version = ? "
                      "order by s.ordering", (sid, version)).fetchall()
    latest = {(r[2], r[9]): r for r in rows}
    out = []
    for (_, tid), r in sorted(latest.items()):
        buttons = db.execute("select ButtonType, ButtonPositions, Minimum, Maximum, Caption from device_templates_buttons "
                             "where id_template = ? order by Ordering, id", (tid,)).fetchall()
        name = r[5] or r[8]
        lab = labels.get(r[8]) or labels.get(name) or []
        controls = []
        chans = [("1", r[3])] if (r[10] or (r[11] or 1) < 2) else [("1", r[3]), ("2", r[4])]
        for ch, vals in chans:
            if not vals:
                continue
            for k, v in enumerate(vals.split("|")):
                try:
                    x = float(v)
                except ValueError:
                    continue
                if x < 0 or k >= len(buttons):
                    continue
                btype, npos, lo, hi, cap = buttons[k]
                label = lab[k] if k < len(lab) else "control %d" % (k + 1)
                if btype in (0, 12) and hi > lo and hi > 100:
                    shown = "%s o'clock" % _clock(x) if btype == 0 else "step %d of %d (%s o'clock)" % (
                        round((x - lo) / ((hi - lo) / max(1, npos - 1))) + 1, npos, _clock(x))
                elif cap and "," in cap:
                    opts = cap.split(",")
                    shown = opts[int(x)] if 0 <= int(x) < len(opts) else str(x)
                else:
                    shown = "on" if x >= 1 and btype == 1 else ("off" if btype == 1 else "%g" % x)
                controls.append({"channel": ch, "control": label, "raw": x, "value": shown})
        out.append({"device": name, "brand": r[7], "model": r[8], "version": r[1], "notes": r[6], "controls": controls})
    return out


@C.command("recall", "recall sessions | show <session> | save <name> [--session S] [--fingerprint [--tracks t1,t2]] | check <name> | list",
           "session recall for a hybrid studio: Live snapshot + outboard settings from SessionRecall (read-only) + "
           "fingerprints of hardware inserts; 'check' shows what changed in Live, the outboard settings to verify by eye, "
           "and the command to re-measure each hardware insert (fingerprint --compare)", "audio")
def c_recall(a):
    op = a[0] if a else "list"
    if op == "sessions":
        db = _sr_db()
        ss = sr_sessions(db)
        return C.out(ss, "\n".join("  %3d %-46s %s v%d" % (s["id"], s["title"][:46], s["created"], s["version"]) for s in ss))
    if op == "show":
        C.need(a, 2, "recall show <session>")
        db = _sr_db()
        s = sr_find(db, " ".join(a[1:]))
        st = sr_settings(db, s["id"])
        if C.JSON:
            return C.out({"session": s, "devices": st})
        print("SessionRecall: %s (v%d, %s)" % (s["title"], s["version"], s["created"]))
        for dv in st:
            print("  %s (%s %s, v%d)%s" % (dv["device"], dv["brand"], dv["model"], dv["version"],
                                          "  notes: " + dv["notes"] if dv["notes"] else ""))
            for c in dv["controls"]:
                print("      ch%s %-18s %s" % (c["channel"], c["control"][:18], c["value"]))
        return
    if op == "list":
        files = sorted(glob.glob(os.path.join(RECALL_DIR, "*.json")))
        rows = [json.load(open(f)) for f in files]
        return C.out([{k: r.get(k) for k in ("name", "time", "session", "fingerprints")} for r in rows],
                     "\n".join("  %-24s %s  SessionRecall: %s  fingerprints: %s" % (r["name"], r["time"],
                               (r.get("session") or {}).get("title"), ", ".join(r.get("fingerprints") or []) or "-") for r in rows)
                     or "no recalls saved")
    if op == "save":
        C.need(a, 2, "recall save <name> [--session S] [--fingerprint [--tracks t1,t2]]")
        name = a[1]
        rec = {"name": name, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "snapshot": C._take(), "fingerprints": []}
        if "--session" in a:
            db = _sr_db()
            s = sr_find(db, a[a.index("--session") + 1])
            rec["session"] = s
            rec["outboard"] = sr_settings(db, s["id"])
        if "--fingerprint" in a:
            # only the tracks asked for; the master only if named: an insert there (a summing mixer, a bus
            # compressor) sends the test signals straight to the speakers
            only = None
            if "--tracks" in a:
                only = {C.find_track(C.tracks(), q)["ref"] for q in a[a.index("--tracks") + 1].split(",")}
            for t in C.tracks():
                if (only is not None and t["ref"] not in only) or (only is None and t["ref"] == "m"):
                    continue
                for d in t["devices"]:
                    if d["class"] == "ProxyAudioEffectDevice" and d["on"]:
                        fn = "%s-%s" % (name, d["name"])
                        c_fingerprint([t["ref"], d["path"], fn])
                        rec["fingerprints"].append(fn)
        os.makedirs(RECALL_DIR, exist_ok=True)
        with open(os.path.join(RECALL_DIR, _safe_name(name) + ".json"), "w") as fh:
            json.dump(rec, fh)
        return C.out({"saved": name, "values": len(rec["snapshot"]["values"]), "session": rec.get("session"),
                      "fingerprints": rec["fingerprints"]},
                     "recall '%s' saved: %d Live values%s%s" % (name, len(rec["snapshot"]["values"]),
                     ", SessionRecall '%s'" % rec["session"]["title"] if rec.get("session") else "",
                     ", %d fingerprints" % len(rec["fingerprints"]) if rec["fingerprints"] else ""))
    if op == "check":
        C.need(a, 2, "recall check <name>")
        rec = json.load(open(os.path.join(RECALL_DIR, _safe_name(a[1]) + ".json")))
        changed, added, removed = C._diff(rec["snapshot"], C._take())
        res = {"name": a[1], "since": rec["time"], "live_changes": [{"what": lab, "then": ot, "now": nt} for _, lab, ot, nt, _, _ in changed],
               "outboard": rec.get("outboard"), "hardware": []}
        for fn in rec.get("fingerprints", []):
            hist = sorted(glob.glob(os.path.join(FP_DIR, "%s--*.json" % _safe_name(fn))))
            if hist:
                res["hardware"].append({"name": fn, "saved": json.load(open(hist[0])).get("time")})
        if C.JSON:
            return C.out(res)
        print("recall '%s' (%s)" % (a[1], rec["time"]))
        print("  Live: %d values changed since then%s" % (len(changed), ": 'live restore' can put them back" if changed else ""))
        for c in res["live_changes"][:30]:
            print("    %-56s %s -> %s" % (c["what"][:56], c["then"], c["now"]))
        if rec.get("outboard"):
            print("  outboard to set by hand (SessionRecall '%s'):" % rec["session"]["title"])
            for dv in rec["outboard"]:
                print("    %s: %s" % (dv["device"], ", ".join("%s %s" % (c["control"], c["value"]) for c in dv["controls"][:12])))
        for h in res["hardware"]:
            print("  hardware '%s' measured on %s: re-measure with 'live fingerprint <track> <device> %s' to compare" % (
                h["name"], h["saved"], h["name"]))
        return
    raise LiveError("usage: live recall sessions|show|save|check|list")
