"""Find tracks, devices and parameters from short, human references."""
import re

from .client import LiveError


def find_track(tracks, q):
    """q: 'm'/'master', 't3', 'r0', a return letter ('A'), an exact or partial track name."""
    q = str(q)
    ql = q.lower()
    if ql in ("m", "master", "main"):
        return next(t for t in tracks if t["ref"] == "m")
    for t in tracks:
        if t["ref"] == ql:
            return t
    if len(q) == 1 and q.isalpha():
        r = "r%d" % (ord(ql) - ord("a"))
        for t in tracks:
            if t["ref"] == r:
                return t
    hit = [t for t in tracks if ql == t["name"].lower()] or [t for t in tracks if ql in t["name"].lower()]
    if not hit:
        raise LiveError("track not found: %s" % q)
    if len(hit) > 1:
        raise LiveError("'%s' matches several tracks: %s" % (q, ", ".join("%s %s" % (t["ref"], t["name"]) for t in hit)))
    return hit[0]


def all_devices(devs):
    for d in devs:
        yield d
        for ch in d.get("chains", []):
            yield from all_devices(ch["devices"])


def find_device(track, q):
    """q: index or path ('2', '2.0.1') or part of the device name (top level first, then inside racks)."""
    q = str(q)
    if re.fullmatch(r"\d+(\.\d+)*", q):
        d = next((x for x in all_devices(track["devices"]) if x["path"] == q), None)
        if d is None:
            raise LiveError("no device %s on %s" % (q, track["name"]))
        return d
    ql = q.lower()
    top = [d for d in track["devices"] if ql == d["name"].lower()] or [d for d in track["devices"] if ql in d["name"].lower()]
    hit = top or [d for d in all_devices(track["devices"]) if ql in d["name"].lower()]
    if len(hit) != 1:
        raise LiveError("device '%s' on %s: %s" % (q, track["name"], "not found" if not hit else
                        "ambiguous, use the path: " + ", ".join("%s %s" % (d["path"], d["name"]) for d in hit)))
    return hit[0]


def find_param(params, q):
    q = str(q)
    hit = ([p for p in params if str(p["i"]) == q] or [p for p in params if p["name"].lower() == q.lower()]
           or [p for p in params if q.lower() in p["name"].lower()])
    if len(hit) != 1:
        raise LiveError("parameter '%s': %s" % (q, "not found" if not hit else
                        "ambiguous: " + ", ".join("%d %s" % (p["i"], p["name"]) for p in hit[:15])))
    return hit[0]


def parse_number(t):
    """'-3 dB' -> -3, '2.5 kHz' -> 2500, '2k5' -> 2500, '-inf dB' -> -inf."""
    t = str(t).strip().lower()
    if "inf" in t:
        return float("-inf") if "-" in t else float("inf")
    mk = re.search(r"(-?\d+)k(\d+)", t)
    if mk:
        return float(mk.group(1) + "." + mk.group(2)) * 1000
    m = re.search(r"-?\d+(?:[.,]\d+)?", t)
    if not m:
        return None
    v = float(m.group(0).replace(",", "."))
    if re.search(r"\d\s*k(hz)?\b", t) or re.search(r"\dk$", t):
        v *= 1000
    return v
