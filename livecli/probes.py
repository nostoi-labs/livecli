"""Client for the Live Probe Protocol (docs/PROTOCOL.md): discover probe servers, read measurements,
compare two probes, summarize a mix. Standard library only."""
import glob
import json
import math
import os
import socket
import time

from .client import LiveError

PROTOCOL_MAJOR = "0"
SOCKET_DIR = os.path.expanduser(os.environ.get("LIVEPROBES_DIR", "~/Library/Application Support/LiveProbes"))
FALLBACK_DIR = "/tmp/LiveProbes-%d" % os.getuid()   # used by servers when SOCKET_DIR makes the path too long
CENTERS = [20.0 * 2 ** (k / 3.0) for k in range(31)]
ZONES = [("sub", 20, 60), ("bass", 60, 120), ("low-mid", 120, 300), ("mid", 300, 1000),
         ("upper-mid", 1000, 4000), ("presence", 4000, 8000), ("air", 8000, 20500)]
SILENCE = -150.0


class Server:
    """One probe server (one socket = one product in one host process)."""

    def __init__(self, path):
        self.path = path
        self.sock = None
        self.buf = b""
        self.rid = 0
        self.info = None

    def connect(self, timeout=2.0):
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(self.path)
        self.sock = s
        self.info = self.request({"cmd": "hello"})
        major = str(self.info.get("protocol", "0")).split(".")[0]
        if major != PROTOCOL_MAJOR:
            raise LiveError("%s speaks protocol %s, this CLI speaks %s.x" % (self.path, self.info.get("protocol"), PROTOCOL_MAJOR))
        return self

    def close(self):
        if self.sock:
            self.sock.close()
            self.sock = None

    def readline(self, timeout=None):
        if timeout is not None:
            self.sock.settimeout(timeout)
        while b"\n" not in self.buf:
            c = self.sock.recv(65536)
            if not c:
                raise LiveError("probe server closed the connection")
            self.buf += c
        line, self.buf = self.buf.split(b"\n", 1)
        return json.loads(line.decode())

    def request(self, req, timeout=5.0):
        self.rid += 1
        req = dict(req, rid=self.rid)
        self.sock.settimeout(timeout)
        self.sock.sendall((json.dumps(req) + "\n").encode())
        while True:
            r = self.readline(timeout)
            if "event" in r:          # pushed while waiting (subscriptions)
                continue
            if r.get("rid") not in (None, self.rid):
                continue
            if not r.get("ok"):
                raise LiveError("probe: %s" % r.get("error"))
            return r

    def has(self, cap):
        return cap in (self.info or {}).get("capabilities", [])


def servers():
    """Connect to every live probe server. Unreachable sockets are skipped."""
    out = []
    dirs = [SOCKET_DIR] if os.environ.get("LIVEPROBES_DIR") else [SOCKET_DIR, FALLBACK_DIR]
    for p in sorted(sum((glob.glob(os.path.join(d, "*.sock")) for d in dirs), [])):
        try:
            out.append(Server(p).connect())
        except (OSError, LiveError, ValueError):
            continue
    return out


def instances(srvs=None):
    """[(server, instance dict)] for every probe instance."""
    out = []
    for s in srvs if srvs is not None else servers():
        for inst in s.request({"cmd": "list"}).get("instances", []):
            inst = dict(inst, product=s.info.get("product"))
            out.append((s, inst))
    return out


def find(insts, q):
    """q: uid, '#<probe_id>', label, or (part of) the track name."""
    q = str(q)
    ql = q.lower()
    if ql.startswith("#") and ql[1:].isdigit():
        hit = [x for x in insts if x[1].get("probe_id") == int(ql[1:])]
    else:
        hit = ([x for x in insts if x[1]["uid"] == q] or
               [x for x in insts if (x[1].get("label") or "").lower() == ql] or
               [x for x in insts if (x[1].get("track") or "").lower() == ql] or
               [x for x in insts if ql in (x[1].get("label") or "").lower() or ql in (x[1].get("track") or "").lower()])
    if not hit:
        raise LiveError("probe not found: %s (see 'live probes')" % q)
    if len(hit) > 1:
        raise LiveError("'%s' matches several probes: %s" % (q, ", ".join(describe(i) for _, i in hit)))
    return hit[0]


def describe(i):
    name = i.get("track") or "?"
    if i.get("label"):
        name += " [%s]" % i["label"]
    return "#%s %s" % (i.get("probe_id", "?"), name)


# ------------------------------------------------------------------ math helpers
def db_to_pow(db):
    return 0.0 if db is None or db <= SILENCE else 10 ** (db / 10.0)


def pow_to_db(p):
    return SILENCE if p <= 0 else 10 * math.log10(p)


def mean_db(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return pow_to_db(sum(db_to_pow(v) for v in vals) / len(vals))


def snapshot_bands(snap, key="mid"):
    b = snap.get("bands") or {}
    return b.get(key)


def average_snapshots(snaps):
    """Average a list of snapshots: band energies in the power domain, peaks as max, the rest as mean."""
    if not snaps:
        return {}
    out = {}
    mids = [snapshot_bands(s, "mid") for s in snaps if snapshot_bands(s, "mid")]
    sides = [snapshot_bands(s, "side") for s in snaps if snapshot_bands(s, "side")]
    if mids:
        out["bands_mid"] = [mean_db([m[k] for m in mids]) for k in range(len(mids[0]))]
    if sides:
        out["bands_side"] = [mean_db([m[k] for m in sides]) for k in range(len(sides[0]))]
    pk = [max(s["peak_db"]) for s in snaps if s.get("peak_db")]
    if pk:
        out["peak_db"] = max(pk)
    rms = [mean_db(s["rms_db"]) for s in snaps if s.get("rms_db")]
    if rms:
        out["rms_db"] = mean_db(rms)
    for k in ("lufs_m", "lufs_s", "correlation", "width", "bass_mono", "crest_db"):
        v = [s[k] for s in snaps if s.get(k) is not None and s.get(k) > SILENCE]
        if v:
            out[k] = (mean_db(v) if k.startswith("lufs") else sum(v) / len(v))
    for k in ("true_peak_dbtp",):
        v = [s[k] for s in snaps if s.get(k) is not None]
        if v:
            out[k] = max(v)
    last = snaps[-1]
    for k in ("lufs_i", "lra", "plr"):
        if last.get(k) is not None:
            out[k] = last[k]
    return out


def average_history(summaries):
    snaps = []
    for s in summaries:
        snap = dict(s)
        if s.get("bands_mid") is not None:
            snap["bands"] = {"mid": s["bands_mid"], "side": s.get("bands_side")}
        snaps.append(snap)
    return average_snapshots(snaps)


def collect(pairs, seconds, hz=10.0, window_s=None):
    """Average measurements of several probes over the next `seconds` (polling `get`)."""
    acc = {i["uid"]: [] for _, i in pairs}
    t_end = time.time() + seconds
    while True:
        for s, i in pairs:
            req = {"cmd": "get", "uid": i["uid"]}
            if window_s:
                req["window_s"] = window_s
            acc[i["uid"]].append(s.request(req)["snapshot"])
        if time.time() >= t_end:
            break
        time.sleep(1.0 / hz)
    return {u: average_snapshots(v) for u, v in acc.items()}


def from_history(pairs, seconds):
    """Average the last `seconds` from each probe's history, on the same 1-second slots."""
    hist = {}
    for s, i in pairs:
        hist[i["uid"]] = {round(x["t"]): x for x in s.request({"cmd": "history", "uid": i["uid"], "seconds": seconds})["summaries"]}
    common = set.intersection(*[set(h) for h in hist.values()]) if hist else set()
    if not common:
        return None
    return {u: average_history([h[t] for t in sorted(common)]) for u, h in hist.items()}, len(common)


def compare(a, b, seconds=10.0, transfer=False, live=False):
    """Band gain from probe a to probe b. a, b: (server, instance).
    Uses the server's own compare when both probes share a server that supports it;
    otherwise history (same seconds), otherwise live polling for `seconds`."""
    (sa, ia), (sb, ib) = a, b
    if not live and sa.path == sb.path and sa.has("compare"):
        try:
            r = sa.request({"cmd": "compare", "a": ia["uid"], "b": ib["uid"], "seconds": seconds,
                            "transfer": bool(transfer)}, timeout=seconds + 10)
            if any(x is not None for x in r.get("gain_db", [])):
                r["method"] = "server"
                return r
        except LiveError:
            pass        # e.g. no common seconds yet: measure here instead
    meas, n = None, 0
    if not live and sa.has("history") and sb.has("history"):
        h = from_history([a, b], int(math.ceil(seconds)))
        if h and h[1] >= max(1, int(seconds * 0.8)):
            meas, n = h
    method = "history"
    if meas is None:
        meas = collect([a, b], seconds)
        method = "poll"
    ma, mb = meas[ia["uid"]], meas[ib["uid"]]
    if not ma.get("bands_mid") or not mb.get("bands_mid"):
        raise LiveError("these probes don't report bands")
    gain = [None if x <= -90 else round(y - x, 2) for x, y in zip(ma["bands_mid"], mb["bands_mid"])]
    out = {"ok": True, "method": method, "seconds": n or seconds, "centers": CENTERS[:len(gain)], "gain_db": gain,
           "a": ma, "b": mb}
    if ma.get("lufs_s") is not None and mb.get("lufs_s") is not None:
        out["loudness_gain_db"] = round(mb["lufs_s"] - ma["lufs_s"], 2)
    elif ma.get("rms_db") is not None and mb.get("rms_db") is not None:
        out["loudness_gain_db"] = round(mb["rms_db"] - ma["rms_db"], 2)
    return out


def regions(centers, gain, base, threshold=1.0):
    """Contiguous runs of bands that differ from `base` by >= threshold dB, with their extreme."""
    out, cur = [], None
    for c, v in zip(centers, gain):
        d = None if v is None else v - base
        sign = 0 if d is None or abs(d) < threshold else (1 if d > 0 else -1)
        if sign and cur and cur["sign"] == sign:
            cur["hi"] = c
            if abs(d) > abs(cur["db"]):
                cur["db"], cur["at"] = d, c
        else:
            if cur:
                out.append(cur)
            cur = {"sign": sign, "lo": c, "hi": c, "db": d, "at": c} if sign else None
    if cur:
        out.append(cur)
    for r in out:
        r["db"] = round(r["db"], 1)
        del r["sign"]
    return out


def zone_energy(bands):
    out = {}
    for name, lo, hi in ZONES:
        vals = [bands[k] for k, c in enumerate(CENTERS[:len(bands)]) if lo <= c < hi]
        out[name] = pow_to_db(sum(db_to_pow(v) for v in vals)) if vals else None
    return out


def fmt_hz(f):
    return ("%.1fk" % (f / 1000)).replace(".0k", "k") if f >= 1000 else "%d" % round(f)
