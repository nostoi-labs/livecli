#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Socket test client for Live Probe servers (protocol v0.1, §1-6).

Checks every command against a running server. Used with tests/vst3host.cpp:

    probe_vst3host ReferenceProbe.vst3 40 sine:1000:-6 sine:1000:-12 anti:-20 silence &
    sleep 6; python3 probe_client_test.py --product reference-probe --testhost

With --testhost it also checks the numbers of that signal set. Exit code 0 = all good.
"""
import argparse
import glob
import json
import math
import os
import socket
import stat
import sys
import time

DIR = os.environ.get("LIVEPROBES_DIR") or os.path.expanduser("~/Library/Application Support/LiveProbes")
fails = 0
checks = 0


def check(cond, what):
    global fails, checks
    checks += 1
    if not cond:
        fails += 1
        print("FAIL", what)
    return cond


def near(a, b, tol, what):
    ok = a is not None and abs(a - b) <= tol
    check(ok, f"{what}: {a} expected {b}±{tol}")
    return ok


class Conn:
    def __init__(self, path):
        self.s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.s.settimeout(3)
        self.s.connect(path)
        self.buf = b""

    def line(self):
        while b"\n" not in self.buf:
            chunk = self.s.recv(65536)
            if not chunk:
                raise EOFError
            self.buf += chunk
        l, self.buf = self.buf.split(b"\n", 1)
        return json.loads(l)

    def req(self, **kw):
        self.s.sendall((json.dumps(kw) + "\n").encode())
        while True:
            m = self.line()
            if "event" not in m:
                return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", default="reference-probe")
    ap.add_argument("--testhost", action="store_true", help="expect the vst3host signal set")
    ap.add_argument("--loudness", action="store_true", help="expect lufs/true peak fields")
    a = ap.parse_args()

    st = os.stat(DIR)
    check(stat.S_IMODE(st.st_mode) == 0o700, f"dir mode {oct(stat.S_IMODE(st.st_mode))}")
    socks = sorted(glob.glob(os.path.join(DIR, a.product + "-*.sock")))
    if not check(socks, f"socket for {a.product} in {DIR}"):
        return 1
    path = socks[-1]
    check(stat.S_IMODE(os.stat(path).st_mode) == 0o600, "socket mode 0600")
    print("socket:", path)
    c = Conn(path)

    h = c.req(cmd="hello", rid=1)
    print("hello:", json.dumps(h))
    check(h["ok"] and h["protocol"] == "0.1" and h["rid"] == 1, "hello")
    check(str(h["pid"]) in path, "pid in socket name")
    caps = h["capabilities"]
    for cap in ("peak", "rms", "history", "subscribe", "reset", "label"):
        check(cap in caps, f"capability {cap}")

    l = c.req(cmd="list")
    insts = l["instances"]
    print(f"list: {len(insts)} instances")
    for i in insts:
        print("  ", json.dumps(i))
        for k in ("uid", "probe_id", "track", "track_index", "track_color", "label", "channels",
                  "sample_rate", "latency_samples", "active", "signal"):
            check(k in i, f"identity field {k}")
        check(i["latency_samples"] == 0, "latency 0")
    if not check(insts, "instances present"):
        return 1
    u = [i["uid"] for i in insts]

    t0 = time.time()
    g = c.req(cmd="get", uid=u[0], window_s=1.0)
    dt = (time.time() - t0) * 1000
    snap = g["snapshot"]
    print(f"get ({dt:.1f} ms):", json.dumps(snap)[:400], "...")
    check(g["ok"] and len(snap["peak_db"]) == insts[0]["channels"], "get peak_db")
    if "bands" in caps:
        b = snap["bands"]
        check(len(b["centers"]) == 31 and len(b["mid"]) == 31, "31 bands")
        centers = [20 * 2 ** (k / 3) for k in range(31)]
        check(all(abs(x - y) < 0.06 for x, y in zip(b["centers"], centers)), "band centers 20·2^(k/3)")
    ga = c.req(cmd="get", all=True)
    check(ga["ok"] and len(ga["snapshots"]) == len(insts), "get all")
    gf = c.req(cmd="get", uid=u[0], fields=["rms_db"])
    check("rms_db" in gf["snapshot"] and "peak_db" not in gf["snapshot"], "get fields")
    gw = c.req(cmd="get", uid=u[0], window_s=99)
    check(gw["snapshot"]["window_s"] == 10.0, "window_s clamped to 10")

    hi = c.req(cmd="history", uid=u[0], seconds=30)
    sums = hi["summaries"]
    print(f"history: {len(sums)} summaries; last:", json.dumps(sums[-1])[:300] if sums else None)
    check(hi["ok"] and len(sums) >= 3, "history has seconds")
    check(all(s["t"] == int(s["t"]) for s in sums), "history t on whole seconds")
    check(all(b["t"] - a_["t"] >= 1 for a_, b in zip(sums, sums[1:])), "history oldest first")
    if len(u) > 1:
        h2 = c.req(cmd="history", uid=u[1], seconds=30)["summaries"]
        common = set(s["t"] for s in sums) & set(s["t"] for s in h2)
        check(len(common) >= 3, f"two instances share second slots ({len(common)})")

    r = c.req(cmd="set_label", uid=u[0], label="pre soothe2", rid="lab")
    check(r["ok"] and r["rid"] == "lab", "set_label")
    check(c.req(cmd="list")["instances"][0]["label"] == "pre soothe2", "label in list")

    e = c.req(cmd="get", uid="nope")
    check(not e["ok"] and e["code"] == "not_found", "not_found")
    e = c.req(cmd="bogus")
    check(not e["ok"] and e["code"] == "unsupported", "unsupported")
    c.s.sendall(b"{nonsense\n")
    e = c.line()
    check(not e["ok"] and e["code"] == "bad_request", "bad json")
    if "compare" not in caps:
        e = c.req(cmd="compare", a=u[0], b=u[-1])
        check(not e["ok"] and e["code"] == "unsupported", "compare unsupported")

    # subscribe: events at the requested rate, field filter, instance_changed on set_label
    s2 = Conn(path)
    r = s2.req(cmd="subscribe", uid=u[0], hz=20, fields=["peak_db", "rms_db"])
    check(r["ok"], "subscribe")
    t0 = time.time()
    n = 0
    got_changed = False
    c.req(cmd="set_label", uid=u[0], label="after")
    while time.time() - t0 < 2.0:
        m = s2.line()
        if m.get("event") == "snapshot":
            n += 1
            check(set(m["snapshot"]) <= {"uid", "t", "window_s", "peak_db", "rms_db"}, "subscribe fields")
        elif m.get("event") == "instance_changed":
            got_changed = got_changed or m["instance"]["label"] == "after"
    rate = n / (time.time() - t0)
    print(f"subscribe: {rate:.1f} snapshots/s at hz=20, instance_changed: {got_changed}")
    check(15 <= rate <= 22, "subscribe rate ~20 Hz")
    check(got_changed, "instance_changed event")
    r = s2.req(cmd="unsubscribe")
    check(r["ok"], "unsubscribe")
    s2.s.close()

    r = c.req(cmd="reset", uid=u[0])
    check(r["ok"] and r["reset"] == [u[0]], "reset")
    hi = c.req(cmd="history", uid=u[0])
    check(len(hi["summaries"]) <= 1, "reset clears history")
    r = c.req(cmd="reset", all=True)
    check(r["ok"] and len(r["reset"]) == len(insts), "reset all")
    c.req(cmd="set_label", uid=u[0], label="")

    if a.testhost and len(insts) >= 4:
        snaps = {s["uid"]: s for s in c.req(cmd="get", all=True, window_s=1.0)["snapshots"]}
        s1, s2_, s3, s4 = (snaps[x] for x in u[:4])
        print("numbers:")
        print(f"  #1 sine 1 kHz -6 dBFS : peak {s1['peak_db']} rms {s1['rms_db']} corr {s1.get('correlation')}")
        near(s1["peak_db"][0], -6.0, 0.05, "#1 peak")
        near(s1["rms_db"][0], -9.0, 0.05, "#1 rms")
        near(s1["correlation"], 1.0, 0.001, "#1 correlation")
        if "bands" in caps:
            print(f"  #1 band 1016 Hz mid {s1['bands']['mid'][17]} (neighbours {s1['bands']['mid'][16]}, {s1['bands']['mid'][18]}) side {s1['bands']['side'][17]}")
            near(s1["bands"]["mid"][17], -9.0, 0.15, "#1 band 1 kHz")
            check(s1["bands"]["side"][17] <= -140, "#1 side silent")
        near(s2_["peak_db"][0], -12.0, 0.05, "#2 peak")
        print(f"  #3 anti-phase noise   : corr {s3.get('correlation')} width {s3.get('width')} side {s3.get('side_db')}")
        near(s3["correlation"], -1.0, 0.001, "#3 correlation")
        near(s3["width"], 1.0, 0.001, "#3 width")
        print(f"  #4 silence            : peak {s4['peak_db']} signal {insts[3]['signal']}")
        near(s4["peak_db"][0], -150.0, 0.01, "#4 silence")
        check(insts[0]["signal"] and not insts[3]["signal"], "signal flags")
        check(all(i["active"] for i in insts), "active")
        check(insts[0]["track"] and insts[0]["track"].startswith("Test 1"), "track name via IInfoListener")
        check(insts[0]["track_index"] == 1 and insts[0]["track_color"] == "#FF8801", "track index/color")
        check(insts[0]["probe_id"] == 101, "probe_id from the parameter")
        g = c.req(cmd="get", probe_id=102)
        check(g["ok"] and g["snapshot"]["uid"] == u[1], "get by probe_id")
        if a.loudness:
            time.sleep(3.5)
            s1 = c.req(cmd="get", uid=u[0])["snapshot"]
            print(f"  #1 loudness: M {s1.get('lufs_m')} S {s1.get('lufs_s')} I {s1.get('lufs_i')} LRA {s1.get('lra')} TP {s1.get('true_peak_dbtp')} PLR {s1.get('plr')}")
            # 1 kHz sine at -6 dBFS on both channels: K-weighting ~ +0.7 dB at 1 kHz → -6.0 - 3.0 + 3.0 - 0.691 + ...
            for k in ("lufs_m", "lufs_s", "lufs_i", "lra", "true_peak_dbtp"):
                check(k in s1, f"loudness field {k}")
            near(s1.get("true_peak_dbtp"), -6.0, 0.1, "#1 true peak")
            near(s1.get("lufs_i"), -6.0 - 3.01 + 3.01 + 0.0, 0.3, "#1 integrated (stereo sine -6 dBFS ≈ -6 LUFS)")
            hist = c.req(cmd="history", uid=u[0], seconds=5)["summaries"]
            check(hist and "lufs_m" in hist[-1] and "true_peak_dbtp" in hist[-1], "loudness in history")

    print(f"{checks} checks, {fails} failures")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
