#!/usr/bin/env python3
"""A fake probe server that speaks the Live Probe Protocol (docs/PROTOCOL.md), for developing and testing
clients without a DAW. Standard library only.

It simulates probe instances with a synthetic, slowly moving pink-ish spectrum. By default:
  #1 "Vox"  [pre]   the source
  #2 "Vox"  [post]  the same signal through a +6 dB bell at 1 kHz (Q 1) and -2 dB overall
  #3 "Bass"         a low-heavy source
  #4 "Master"       sum of all

Usage: mock_probe.py [--no-compare] [--no-history] [--product NAME]
Socket: $LIVEPROBES_DIR or ~/Library/Application Support/LiveProbes/<product>-<pid>.sock
"""
import json
import math
import os
import random
import socket
import sys
import threading
import time

PROTOCOL = "0.1"
CENTERS = [20.0 * 2 ** (k / 3.0) for k in range(31)]
SILENCE = -150.0


def bell(f, f0=1000.0, gain_db=6.0, q=1.0):
    """Magnitude (dB) of an RBJ peaking EQ at frequency f (good enough for a mock)."""
    x = math.log2(f / f0)
    bw = 1.0 / q                      # ~octaves
    return gain_db * math.exp(-(x / (bw * 0.6)) ** 2)


def p2db(p):
    return SILENCE if p <= 0 else 10 * math.log10(p)


def db2p(d):
    return 0.0 if d <= SILENCE else 10 ** (d / 10.0)


class Instance:
    def __init__(self, uid, probe_id, track, label, shape):
        self.uid, self.probe_id, self.track, self.label, self.shape = uid, probe_id, track, label, shape
        self.history = []          # 1-second summaries
        self.tp_max = SILENCE
        self.lufs_hist = []
        self.t_reset = time.monotonic()

    def identity(self):
        return {"uid": self.uid, "probe_id": self.probe_id, "track": self.track, "track_index": None,
                "track_color": None, "label": self.label, "channels": 2, "sample_rate": 48000.0,
                "latency_samples": 0, "active": True, "signal": True}


class MockServer:
    def __init__(self, product="mock-probe", compare=True, history=True):
        self.product, self.can_compare, self.can_history = product, compare, history
        d = os.path.expanduser(os.environ.get("LIVEPROBES_DIR", "~/Library/Application Support/LiveProbes"))
        os.makedirs(d, mode=0o700, exist_ok=True)
        self.path = os.path.join(d, "%s-%d.sock" % (product, os.getpid()))
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.inst = [
            Instance("mk-0001", 1, "Vox", "pre", "vox"),
            Instance("mk-0002", 2, "Vox", "post", "vox_eq"),
            Instance("mk-0003", 3, "Bass", "", "bass"),
            Instance("mk-0004", 4, "Master", "", "master"),
        ]
        self.subs = []             # (conn, uid, hz, last_sent)
        self.running = True

    # ------------------------------------------------------------- synthetic signal
    def base(self, t, shape):
        # the same "music" for every instance at the same moment (like probes on one signal path)
        wob = 1.5 * math.sin(t * 0.7) + random.Random(int(t * 10)).gauss(0, 0.4)
        if shape in ("vox", "vox_eq"):
            b = [-40 - 3 * abs(math.log2(c / 1500.0)) ** 1.6 + wob for c in CENTERS]
            b = [v if c > 90 else v - 30 for v, c in zip(b, CENTERS)]
            if shape == "vox_eq":
                b = [v + bell(c) - 2.0 for v, c in zip(b, CENTERS)]
        elif shape == "bass":
            b = [-30 - 10 * max(0, math.log2(c / 80.0)) + wob for c in CENTERS]
        else:
            parts = [self.base(t, s) for s in ("vox_eq", "bass")]
            b = [p2db(sum(db2p(p[k]) for p in parts)) + 1.0 for k in range(31)]
        return b

    def snapshot(self, i, window_s=0.3):
        t = time.monotonic() - self.t0
        mid = self.base(t, i.shape)
        side = [v - 12 for v in mid]
        total = p2db(sum(db2p(v) for v in mid) + sum(db2p(v) for v in side))
        rms = total
        peak = rms + 12 + random.gauss(0, 0.5)
        lufs_m = rms + 0.7
        i.tp_max = max(i.tp_max, peak + 0.3)
        i.lufs_hist.append(lufs_m)
        lufs_i = p2db(sum(db2p(x) for x in i.lufs_hist[-3000:]) / len(i.lufs_hist[-3000:]))
        return {"uid": i.uid, "t": round(t, 3), "window_s": window_s,
                "peak_db": [round(peak, 1), round(peak - 0.3, 1)], "rms_db": [round(rms, 1), round(rms - 0.2, 1)],
                "true_peak_dbtp": round(i.tp_max, 1), "lufs_m": round(lufs_m, 1), "lufs_s": round(lufs_m - 0.3, 1),
                "lufs_i": round(lufs_i, 1), "lra": 3.5, "plr": round(i.tp_max - lufs_i, 1), "crest_db": round(peak - rms, 1),
                "correlation": 0.85, "width": 0.06, "mid_db": round(p2db(sum(db2p(v) for v in mid)), 1),
                "side_db": round(p2db(sum(db2p(v) for v in side)), 1),
                "bands": {"centers": [round(c, 1) for c in CENTERS], "mid": [round(v, 1) for v in mid],
                          "side": [round(v, 1) for v in side]},
                "bass_mono": 0.98, "since_reset_s": round(time.monotonic() - i.t_reset, 1)}

    def tick_history(self):
        while self.running:
            sec = math.floor(time.monotonic() - self.t0)
            with self.lock:
                for i in self.inst:
                    if not i.history or i.history[-1]["t"] < sec:
                        s = self.snapshot(i)
                        i.history.append({"t": float(sec), "peak_db": s["peak_db"], "rms_db": s["rms_db"],
                                          "lufs_m": s["lufs_m"], "lufs_s": s["lufs_s"],
                                          "true_peak_dbtp": s["true_peak_dbtp"], "correlation": s["correlation"],
                                          "bands_mid": s["bands"]["mid"], "bands_side": s["bands"]["side"]})
                        i.history = i.history[-120:]
            self.push()
            time.sleep(0.05)

    def push(self):
        now = time.monotonic()
        for sub in list(self.subs):
            conn, uid, hz, last = sub
            if now - last[0] >= 1.0 / hz:
                last[0] = now
                i = self.get_inst(uid)
                try:
                    conn.sendall((json.dumps({"event": "snapshot", "uid": uid, "snapshot": self.snapshot(i)}) + "\n").encode())
                except OSError:
                    self.subs.remove(sub)

    # ------------------------------------------------------------- protocol
    def get_inst(self, uid=None, probe_id=None):
        for i in self.inst:
            if (uid is not None and i.uid == uid) or (probe_id is not None and i.probe_id == int(probe_id)):
                return i
        raise KeyError("no instance %s" % (uid or probe_id))

    def caps(self):
        c = ["peak", "rms", "true_peak", "lufs", "lra", "bands", "stereo", "reset", "subscribe", "label"]
        if self.can_history:
            c.append("history")
        if self.can_compare:
            c.append("compare")
        c.append("onsets")
        return c

    def handle(self, req, conn):
        cmd = req.get("cmd")
        if cmd == "hello":
            return {"protocol": PROTOCOL, "product": self.product, "version": "0.1.0", "pid": os.getpid(),
                    "host": "mock", "capabilities": self.caps()}
        if cmd == "list":
            return {"instances": [i.identity() for i in self.inst]}
        if cmd == "get":
            if req.get("all"):
                return {"snapshots": [self.snapshot(i, req.get("window_s", 0.3)) for i in self.inst]}
            return {"snapshot": self.snapshot(self.get_inst(req.get("uid"), req.get("probe_id")), req.get("window_s", 0.3))}
        if cmd == "history":
            if not self.can_history:
                raise ValueError("unsupported")
            i = self.get_inst(req.get("uid"))
            n = int(req.get("seconds", 30))
            return {"uid": i.uid, "summaries": i.history[-n:]}
        if cmd == "compare":
            if not self.can_compare:
                raise ValueError("unsupported")
            a, b = self.get_inst(req["a"]), self.get_inst(req["b"])
            n = int(req.get("seconds", 10))
            ha = {h["t"]: h for h in a.history[-n:]}
            hb = {h["t"]: h for h in b.history[-n:]}
            ts = sorted(set(ha) & set(hb))
            def avg(h, k):
                return p2db(sum(db2p(h[t]["bands_mid"][k]) for t in ts) / len(ts))
            gain = [round(avg(hb, k) - avg(ha, k), 2) for k in range(31)] if ts else [None] * 31
            return {"seconds": len(ts), "centers": [round(c, 1) for c in CENTERS], "gain_db": gain}
        if cmd == "onsets":
            # synthetic: one onset per 1/16 at 120 BPM, offset by the instance's timing (post = +10 ms, bass = +3 ms)
            i = self.get_inst(req.get("uid"), req.get("probe_id"))
            sec = float(req.get("seconds", 8))
            late_ms = {"vox": 0.0, "vox_eq": 10.0, "bass": 3.0, "master": 0.0}[i.shape]
            tempo, t0 = 120.0, time.monotonic() - self.t0
            ppq0 = t0 * tempo / 60.0
            on = []
            k = math.ceil(ppq0 / 0.25)
            while (k * 0.25 - ppq0) * 60.0 / tempo < sec:
                jitter = random.gauss(0, 0.1)
                ppq = k * 0.25 + (late_ms + jitter) / 1000.0 * tempo / 60.0
                on.append({"t": round(t0 + (k * 0.25 - ppq0) * 0.5, 4), "ppq": round(ppq, 6), "tempo": tempo, "level_db": -8.0})
                k += 1
            return {"uid": i.uid, "sample_rate": 48000.0, "seconds": sec, "onsets": on}
        if cmd == "subscribe":
            self.subs.append((conn, req["uid"], float(req.get("hz", 10)), [0.0]))
            return {}
        if cmd == "unsubscribe":
            self.subs = [s for s in self.subs if not (s[0] is conn and s[1] == req.get("uid"))]
            return {}
        if cmd == "reset":
            for i in (self.inst if req.get("all") else [self.get_inst(req.get("uid"))]):
                i.tp_max, i.lufs_hist, i.history, i.t_reset = SILENCE, [], [], time.monotonic()
            return {}
        if cmd == "set_label":
            self.get_inst(req["uid"]).label = req.get("label", "")
            return {}
        raise ValueError("unknown command: %s" % cmd)

    def client(self, conn):
        buf = b""
        try:
            while self.running:
                c = conn.recv(65536)
                if not c:
                    break
                buf += c
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        req = json.loads(line.decode())
                        with self.lock:
                            res = dict(self.handle(req, conn), ok=True)
                    except KeyError as e:
                        res = {"ok": False, "error": str(e), "code": "not_found"}
                    except Exception as e:
                        res = {"ok": False, "error": str(e), "code": "bad_request"}
                    res["rid"] = req.get("rid") if isinstance(req, dict) else None
                    conn.sendall((json.dumps(res) + "\n").encode())
        except OSError:
            pass
        finally:
            self.subs = [s for s in self.subs if s[0] is not conn]
            conn.close()

    def serve(self):
        if os.path.exists(self.path):
            os.unlink(self.path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(self.path)
        os.chmod(self.path, 0o600)
        srv.listen(8)
        threading.Thread(target=self.tick_history, daemon=True).start()
        import signal
        signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        print("mock probe server on %s" % self.path, flush=True)
        try:
            while True:
                conn, _ = srv.accept()
                threading.Thread(target=self.client, args=(conn,), daemon=True).start()
        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            srv.close()
            os.unlink(self.path)


if __name__ == "__main__":
    a = sys.argv[1:]
    prod = a[a.index("--product") + 1] if "--product" in a else "mock-probe"
    MockServer(prod, compare="--no-compare" not in a, history="--no-history" not in a).serve()
