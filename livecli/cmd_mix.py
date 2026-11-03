"""Mix tools built on probes that no other tool has: level-matched A/B, gain staging, phase/polarity between tracks,
the measured effect of changes, live reference comparison, set health check."""
import contextlib
import io
import json
import os
import statistics
import time

from . import cli as C
from . import probes as P
from .client import CONFIG_DIR, LiveError, call, config

STATE = os.path.join(CONFIG_DIR, "abx-state.json")


# ------------------------------------------------------------------ probe placement helpers
def _quiet(fn, *args):
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args)


def ensure_probe(ref, where="end", label="lc"):
    """A probe on track `ref` at `where`: 'end', ('after', path) or ('before', path) of a top-level device.
    Reuses a probe already there. Returns ((server, instance), added: bool)."""
    devs = C.track(ref)["devices"]
    if where == "end":
        pos = devs[-1]["path"] if devs else None
        args = [ref, label]
    elif where[0] == "after":
        pos = str(int(where[1]) + 1) if int(where[1]) + 1 < len(devs) else None
        args = [ref, "after", where[1], label]
        pos_existing = pos
    else:
        i = int(where[1])
        pos = str(i - 1) if i > 0 else None
        args = [ref, "first", label] if i == 0 else [ref, "after", str(i - 1), label]
    if pos is not None:
        found = C._find_probe_at(ref, pos)
        if found:
            return found, False
    _quiet(C.c_probe_add, args)
    devs = C.track(ref)["devices"]
    if where == "end":
        at = devs[-1]["path"]
    elif where[0] == "after":
        at = str(int(where[1]) + 1)
    else:
        at = where[1]          # the probe took the device's place; the device moved one right
    found = C._find_probe_at(ref, at)
    if not found:
        raise LiveError("probe not found after loading it on %s" % ref, code="probe")
    return found, True


def remove_added(labels_by_track):
    for ref, labels in labels_by_track.items():
        cur = C.track(ref)["devices"]
        for k in range(len(cur) - 1, -1, -1):
            x = C._find_probe_at(ref, cur[k]["path"])
            if x and x[1].get("label") in labels:
                call("delete_device", track=ref, device=cur[k]["path"])


def loudness(m):
    return m.get("lufs_s") if m.get("lufs_s") is not None else m.get("rms_db")


# ------------------------------------------------------------------ B1: level-matched A/B
def _abx_state():
    try:
        return json.load(open(STATE))
    except (OSError, ValueError):
        return {}


def _abx_save(st):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(STATE, "w") as fh:
        json.dump(st, fh, indent=1)


def _abx_set(t, d, side, use_ab):
    if use_ab:
        call("ab", track=t["ref"], device=d["path"], op=side)
    else:
        call("device_on", track=t["ref"], device=d["path"], on=(side == "a"))


def _match_utility(ref, create=True):
    devs = C.track(ref)["devices"]
    u = next((x for x in devs if x["name"] == "LiveCLI Match"), None)
    if u or not create:
        return u
    call("insert_device", track=ref, name="Utility", index=len(devs))
    path = str(len(devs))
    call("rename_device", track=ref, device=path, name="LiveCLI Match")
    return next(x for x in C.track(ref)["devices"] if x["name"] == "LiveCLI Match")


def _set_utility_gain(ref, path, db):
    ps = call("params", track=ref, device=path)["params"]
    g = next(p for p in ps if p["name"] == "Gain")
    call("set_param", track=ref, device=path, param=g["i"], text="%.2f dB" % db)


@C.command("abx", "abx <track> <device> [seconds] [--ab] | a | b | done", "level-matched A/B: measure the device on vs "
           "off (or its A/B slots with --ab) with a probe at the end of the track, then 'a'/'b' switch with a "
           "compensating Utility so both play equally loud (the louder side is turned down); 'done' restores", "probes")
def c_abx(a):
    C.need(a, 2, "abx <track> <device> [seconds] [--ab] | a | b | done")
    t = C.track(a[0])
    d = C.find_device(t, a[1])
    key = "%s|%s|%s" % (t["ref"], d["path"], d["name"])
    st = _abx_state()
    op = a[2] if len(a) > 2 and a[2] in ("a", "b", "done") else "measure"
    if op == "measure":
        nums = [x for x in a[2:] if not x.startswith("--")]
        sec = float(nums[0]) if nums else 6.0
        use_ab = "--ab" in a
        if not call("song")["playing"]:
            raise LiveError("start playback first (loop a section)")
        pr, added = ensure_probe(t["ref"], "end", "abx")
        orig = {"on": d["on"], "ab": d.get("ab")}
        res = {}
        try:
            for side in ("a", "b"):
                _abx_set(t, d, side, use_ab)
                time.sleep(0.8)
                res[side] = P.collect([pr], sec)[pr[1]["uid"]]
        finally:
            if use_ab:
                call("ab", track=t["ref"], device=d["path"], op=(orig["ab"] or "A").lower())
            else:
                call("device_on", track=t["ref"], device=d["path"], on=bool(orig["on"]))
        la, lb = loudness(res["a"]), loudness(res["b"])
        diff = round(la - lb, 2)
        st[key] = {"diff_db": diff, "ab": use_ab, "orig": orig, "probe_added": added,
                   "measured": time.strftime("%Y-%m-%d %H:%M:%S")}
        _abx_save(st)
        what = ("slot A", "slot B") if use_ab else ("on", "off")
        return C.out(dict(st[key], a=res["a"], b=res["b"]),
                     "%s / %s: %s %.1f, %s %.1f (%s) -> %s is %.1f dB louder.\n"
                     "  'live abx %s %s a' and '... b' switch at equal loudness; '... done' restores."
                     % (t["name"], d["name"], what[0], la, what[1], lb, "LUFS-S" if res["a"].get("lufs_s") is not None else "RMS",
                        what[0] if diff > 0 else what[1], abs(diff), a[0], a[1]))
    if key not in st:
        raise LiveError("measure first: live abx %s %s" % (a[0], a[1]))
    s = st[key]
    if op in ("a", "b"):
        u = _match_utility(t["ref"])
        _abx_set(t, d, op, s["ab"])
        # turn the louder side down to the quieter one
        gain = -max(0.0, s["diff_db"]) if op == "a" else -max(0.0, -s["diff_db"])
        _set_utility_gain(t["ref"], u["path"], gain)
        return C.out({"side": op, "gain_db": gain}, "%s: %s, match gain %+.1f dB" % (t["name"], op.upper(), gain))
    u = _match_utility(t["ref"], create=False)
    if u:
        call("delete_device", track=t["ref"], device=u["path"])
    if s["ab"]:
        call("ab", track=t["ref"], device=d["path"], op=(s["orig"].get("ab") or "A").lower())
    else:
        call("device_on", track=t["ref"], device=d["path"], on=bool(s["orig"]["on"]))
    if s.get("probe_added"):
        remove_added({t["ref"]: ["abx"]})
    del st[key]
    _abx_save(st)
    C.out(True, "restored")


# ------------------------------------------------------------------ B2: gain staging
def _gs_cfg():
    g = config().get("gainstage", {})
    return {"vu_ref": float(g.get("vu_ref_dbfs", -18)), "dbu0": float(g.get("dbu_at_0dbfs", 24)),
            "peak_max": float(g.get("peak_max_dbfs", -6)), "vu_target": float(g.get("vu_target", 0)),
            "vu_low": float(g.get("vu_low", -8)), "vu_hot": float(g.get("vu_hot", 3)),
            "by_device": g.get("vu_ref_by_device", {})}


def _verdict(peak, vu, cfg):
    if peak is not None and peak > cfg["peak_max"]:
        return "hot peaks"
    if vu is not None and vu > cfg["vu_hot"]:
        return "hot"
    if vu is not None and vu < cfg["vu_low"]:
        return "low"
    return "ok"


@C.command("gainstage", "gainstage [seconds] [--tracks t1,t2|all] [--hardware]", "levels of tracks (probe at the end) "
           "and, with --hardware, into every External Audio Effect (probe right before it): peak, RMS, VU and dBu with "
           "the interface calibration in config 'gainstage' (vu_ref_dbfs, dbu_at_0dbfs, peak_max_dbfs, vu_ref_by_device)",
           "probes")
def c_gainstage(a):
    cfg = _gs_cfg()
    nums = [x for x in a if not x.startswith("--") and (a.index(x) == 0 or a[a.index(x) - 1] != "--tracks")]
    sec = float(nums[0]) if nums and nums[0].replace(".", "").isdigit() else 10.0
    if not call("song")["playing"]:
        raise LiveError("start playback first (the loudest part of the song)")
    ts = C.tracks()
    if "--tracks" in a:
        q = a[a.index("--tracks") + 1]
        refs = [t["ref"] for t in ts if t["ref"][0] == "t"] if q == "all" else [C.find_track(ts, x)["ref"] for x in q.split(",")]
    elif "--hardware" in a:
        refs = []
    else:
        refs = [t["ref"] for t in ts if t["ref"][0] == "t"]
    targets, added = [], {}
    for ref in refs:
        pr, ad = ensure_probe(ref, "end", "gs")
        targets.append(("track", ref, C.find_track(ts, ref)["name"], None, pr))
        if ad:
            added.setdefault(ref, []).append("gs")
    if "--hardware" in a:
        for t in ts:
            for d in t["devices"]:
                if d["class"] == "ProxyAudioEffectDevice" and d["on"]:
                    pr, ad = ensure_probe(t["ref"], ("before", d["path"]), "gs-hw")
                    targets.append(("into", t["ref"], t["name"], d["name"], pr))
                    if ad:
                        added.setdefault(t["ref"], []).append("gs-hw")
    try:
        meas = P.collect([x[4] for x in targets], sec)
    finally:
        remove_added(added)
    rows = []
    for kind, ref, tname, dname, pr in targets:
        m = meas[pr[1]["uid"]]
        peak, rms = m.get("peak_db"), m.get("rms_db")
        vref = cfg["vu_ref"]
        for k, v in cfg["by_device"].items():
            if dname and k.lower() in dname.lower():
                vref = float(v)
        vu = None if rms is None else rms - vref
        rows.append({"kind": kind, "track": tname, "device": dname, "peak_dbfs": peak, "rms_dbfs": rms, "vu": vu,
                     "vu_ref_dbfs": vref, "peak_dbu": None if peak is None else peak + cfg["dbu0"],
                     "verdict": _verdict(peak, vu, cfg), "change_db": None if vu is None else round(cfg["vu_target"] - vu, 1)})
    if C.JSON:
        return C.out({"seconds": sec, "config": cfg, "levels": rows})
    print("levels over %gs (VU = RMS vs ref, 0 dBFS = %+g dBu, peaks max %g dBFS)" % (sec, cfg["dbu0"], cfg["peak_max"]))
    for r in rows:
        where = r["track"] if r["kind"] == "track" else "%s -> %s" % (r["track"], r["device"])
        adv = "" if r["verdict"] == "ok" else "  (%s: %+.1f dB)" % (r["verdict"], r["change_db"])
        print("  %-44s peak %6.1f dBFS (%+5.1f dBu)  VU %+5.1f%s" % (where[:44], r["peak_dbfs"], r["peak_dbu"], r["vu"], adv))


# ------------------------------------------------------------------ B4: phase and polarity
@C.command("phase", "phase <track-a> <track-b> [seconds]", "phase relation of two tracks playing related material "
           "(kick and bass, two mics, layers): delay, polarity, correlation, with advice", "probes")
def c_phase(a):
    C.need(a, 2, "phase <track-a> <track-b> [seconds]")
    sec = float(a[2]) if len(a) > 2 else 5.0
    if not call("song")["playing"]:
        raise LiveError("start playback first")
    ta, tb = C.track(a[0]), C.track(a[1])
    pa, ada = ensure_probe(ta["ref"], "end", "phase")
    pb, adb = ensure_probe(tb["ref"], "end", "phase")
    try:
        if pa[0].path != pb[0].path or not pa[0].has("delay"):
            raise LiveError("both probes must be the same product with 'delay' (Reference/Loudline Probe 0.1.1+)", code="probe")
        r = pa[0].request({"cmd": "delay", "a": pa[1]["uid"], "b": pb[1]["uid"], "seconds": sec, "max_lag_ms": 50},
                          timeout=sec + 10)
    finally:
        added = {}
        if ada:
            added.setdefault(ta["ref"], []).append("phase")
        if adb:
            added.setdefault(tb["ref"], []).append("phase")
        remove_added(added)
    res = dict(r, track_a=ta["name"], track_b=tb["name"])
    adv = []
    if (r.get("correlation_peak") or 0) < 0.3:
        adv.append("low correlation: different material, the phase relation is not meaningful here")
    else:
        if r.get("polarity") == -1:
            adv.append("polarity is inverted: flip %s (Utility: Phz-L and Phz-R)" % tb["name"])
        if abs(r.get("delay_ms") or 0) >= 0.05:
            late, early = (tb["name"], ta["name"]) if r["delay_ms"] > 0 else (ta["name"], tb["name"])
            adv.append("%s is %.2f ms late: delay %s by %.2f ms (Align Delay or Track Delay) to line them up"
                       % (late, abs(r["delay_ms"]), early, abs(r["delay_ms"])))
        if not adv:
            adv.append("in phase and aligned")
    res["advice"] = adv
    if C.JSON:
        return C.out(res)
    print("%s vs %s: delay %+.2f ms (%d samples), polarity %s, correlation %.2f, ambiguity %.2f" % (
        ta["name"], tb["name"], r["delay_ms"], r["delay_samples"], "+" if r.get("polarity", 1) > 0 else "inverted",
        r.get("correlation_peak") or 0, r.get("ambiguity") or 0))
    for x in adv:
        print("  " + x)


# ------------------------------------------------------------------ B5: the measured effect of changes
def measure_all_probes(sec):
    srvs = P.servers()
    insts = P.instances(srvs) if srvs else []
    if not insts:
        return {}
    meas = P.collect(insts, sec)
    return {P.describe(i): meas[i["uid"]] for _, i in insts}


@C.command("explain", "explain [snapshot] [seconds]", "what changed since a snapshot taken with --measure, and what it "
           "did to the sound: parameter changes plus loudness and band changes at every probe", "probes")
def c_explain(a):
    snap_dir = os.path.join(CONFIG_DIR, "snapshots")
    import glob
    files = sorted(glob.glob(os.path.join(snap_dir, "*.json")), key=os.path.getmtime)
    if a and not a[0].replace(".", "").isdigit():
        path = os.path.join(snap_dir, a[0] + ".json")
        rest = a[1:]
    else:
        if not files:
            raise LiveError("no snapshot: 'live snapshot <name> --measure' first")
        path, rest = files[-1], a
    sec = float(rest[0]) if rest else 5.0
    old = json.load(open(path))
    if not old.get("measure"):
        raise LiveError("this snapshot has no measurements: take it with 'live snapshot <name> --measure'")
    new_vals = C._take()
    changed, added, removed = C._diff(old, new_vals)
    now = measure_all_probes(sec)
    sound = []
    for name, m0 in old["measure"].items():
        m1 = now.get(name)
        if not m1:
            continue
        e = {"probe": name, "loudness_db": None, "regions": []}
        l0, l1 = loudness(m0), loudness(m1)
        if l0 is not None and l1 is not None:
            e["loudness_db"] = round(l1 - l0, 2)
        if m0.get("bands_mid") and m1.get("bands_mid"):
            g = [None if x <= -90 else y - x for x, y in zip(m0["bands_mid"], m1["bands_mid"])]
            base = statistics.median([x for x in g if x is not None]) if any(x is not None for x in g) else 0
            e["regions"] = P.regions(P.CENTERS, g, base)
        for k in ("true_peak_dbtp", "plr", "width", "correlation"):
            if m0.get(k) is not None and m1.get(k) is not None:
                e[k] = round(m1[k] - m0[k], 2)
        sound.append(e)
    res = {"since": old["time"], "changes": [{"what": lab, "from": ot, "to": nt} for _, lab, ot, nt, _, _ in changed],
           "sound": sound}
    if C.JSON:
        return C.out(res)
    print("since %s: %d parameter changes" % (old["time"], len(changed)))
    for c in res["changes"]:
        print("  %-58s %s -> %s" % (c["what"][:58], c["from"], c["to"]))
    for e in sound:
        bits = []
        if e["loudness_db"] is not None:
            bits.append("loudness %+.1f dB" % e["loudness_db"])
        for k, lab in (("true_peak_dbtp", "TP"), ("plr", "PLR"), ("width", "width")):
            if e.get(k):
                bits.append("%s %+.2f" % (lab, e[k]))
        print("  %s: %s" % (e["probe"], ", ".join(bits) or "no change"))
        for r in e["regions"]:
            print("      %s %+.1f dB around %s Hz (%s-%s)" % ("more" if r["db"] > 0 else "less", r["db"], P.fmt_hz(r["at"]),
                                                          P.fmt_hz(r["lo"]), P.fmt_hz(r["hi"])))


# ------------------------------------------------------------------ B6: reference comparison, live
@C.command("refcompare", "refcompare <mix-probe> <ref-probe> [seconds]", "your mix against a reference playing in Live "
           "(a track with the reference, probe on it, muted or sends-only): level-matched tonal balance per band, "
           "loudness, PLR, width, low-end mono", "probes")
def c_refcompare(a):
    C.need(a, 2, "refcompare <mix-probe> <ref-probe> [seconds]")
    sec = float(a[2]) if len(a) > 2 else 10.0
    _, insts = C._probe_pairs()
    pm, pr = P.find(insts, a[0]), P.find(insts, a[1])
    meas = P.collect([pm, pr], sec)
    mm, mr = meas[pm[1]["uid"]], meas[pr[1]["uid"]]
    lm, lr = loudness(mm), loudness(mr)
    off = (lm - lr) if lm is not None and lr is not None else 0.0
    raw = [None if (x <= -90 or y <= -90) else x - y for x, y in zip(mm["bands_mid"], mr["bands_mid"])]
    # tonal balance: matched on the median band difference, so one strong band can't hide itself in the matching
    valid = [x for x in raw if x is not None]
    tonal = statistics.median(valid) if valid else 0.0
    g = [None if x is None else round(x - tonal, 2) for x in raw]
    regions = P.regions(P.CENTERS, g, 0.0, threshold=1.5)
    res = {"seconds": sec, "loudness_diff_db": round(off, 2), "gain_db": g, "regions": regions, "mix": mm, "reference": mr}
    for k in ("lufs_s", "true_peak_dbtp", "plr", "crest_db", "width", "correlation", "bass_mono"):
        if mm.get(k) is not None and mr.get(k) is not None:
            res[k] = {"mix": mm[k], "reference": mr[k]}
    if C.JSON:
        return C.out(res)
    print("mix (%s) vs reference (%s), %gs, level-matched (mix is %+.1f dB louder)" % (
        P.describe(pm[1]), P.describe(pr[1]), sec, off))
    for k, lab in (("lufs_s", "LUFS-S"), ("true_peak_dbtp", "true peak"), ("plr", "PLR"), ("crest_db", "crest"),
                   ("width", "width"), ("bass_mono", "low-end mono")):
        if k in res:
            print("  %-12s mix %7.2f   reference %7.2f" % (lab, res[k]["mix"], res[k]["reference"]))
    if not regions:
        print("  tonal balance: within 1.5 dB of the reference in every band")
    for r in regions:
        print("  %s %+.1f dB around %s Hz (%s-%s Hz) compared to the reference" % (
            "more" if r["db"] > 0 else "less", r["db"], P.fmt_hz(r["at"]), P.fmt_hz(r["lo"]), P.fmt_hz(r["hi"])))


# ------------------------------------------------------------------ B8: set health check
@C.command("check", "check [--probes seconds]", "health of the open set: armed or auto-armed tracks, tracks still on "
           "session clips, overridden automation, heavy look-ahead plugins on, devices with latency, CPU per track, "
           "default names; with probes, clipping and true peak; for a saved set also the hardware I/O lint", "info")
def c_check(a):
    issues = []
    s = call("song")
    ts = C.tracks()
    for t in ts:
        if t.get("arm") or t.get("implicit_arm"):
            issues.append(("note", t["name"], "armed%s" % (" (automatic arm of the selected MIDI track)" if t.get("implicit_arm") and not t.get("arm") else "")))
        if __import__("re").fullmatch(r"\d+-(Audio|MIDI)", t["name"]):
            issues.append(("note", t["name"], "default name"))
    try:
        for x in call("automation", timeout=20):
            if x["state"] == "overridden":
                issues.append(("warning", x["track_name"], "automation overridden: %s (live re-enable-automation)" % x["param"]))
    except LiveError:
        pass
    inv = None
    try:
        from .cli import plugin_inventory
        inv = {(p["name"]): p for p in plugin_inventory()["plugins"]}
    except Exception:
        pass
    for t in ts:
        for d in C.all_devices(t["devices"]):
            if d.get("latency_samples", 0) >= 1024:
                issues.append(("note", t["name"], "%s adds %d samples of latency (%.1f ms)" % (d["name"], d["latency_samples"], d.get("latency_ms", 0))))
            if d["on"] and inv:
                p = next((v for k, v in inv.items() if k and k.lower() in d["name"].lower()), None)
                note = (p or {}).get("notes", "")
                if any(w in note.lower() for w in ("look-ahead", "heavy", "crackle")):
                    issues.append(("note", t["name"], "%s is on: %s" % (d["name"], note)))
    top = sorted(ts, key=lambda t: -(t.get("cpu_impact") or 0))[:3]
    cpu = call("cpu")
    if cpu.get("peak_process_usage", 0) and cpu["peak_process_usage"] > 80:
        issues.append(("warning", "-", "Live CPU peak %.0f%%: heaviest tracks %s" % (
            cpu["peak_process_usage"], ", ".join("%s %.1f%%" % (t["name"], 100 * (t.get("cpu_impact") or 0)) for t in top))))
    if "--probes" in a:
        sec = float(a[a.index("--probes") + 1]) if len(a) > a.index("--probes") + 1 else 3.0
        for name, m in measure_all_probes(sec).items():
            if (m.get("peak_db") or -150) > -0.1:
                issues.append(("warning", name, "clipping: sample peak %.1f dBFS" % m["peak_db"]))
            if (m.get("true_peak_dbtp") or -150) > -1.0:
                issues.append(("warning", name, "true peak %.1f dBTP (above -1)" % m["true_peak_dbtp"]))
    fp = s.get("file_path")
    if fp and os.path.exists(fp):
        from . import cmd_offline
        for lvl, ref, name, msg in cmd_offline.lint(cmd_offline.parse_als(fp)):
            if "hardware" in name or "missing sample" in msg:
                issues.append(("warning", name, msg + " (saved set)"))
    if C.JSON:
        return C.out({"issues": [dict(zip(("level", "where", "issue"), i)) for i in issues]})
    if not issues:
        return print("no issues found")
    for lvl, where, msg in sorted(issues, key=lambda i: i[0] != "warning"):
        print("  %-7s %-26s %s" % (lvl, where[:26], msg))
