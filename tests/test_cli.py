"""End-to-end tests: the real CLI talks to the real commands.py running on a fake Live API,
and to the mock probe server. Run: python3 -m unittest discover -s tests"""
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()
os.environ["LIVECLI_PORT"] = str(PORT)
TMP = tempfile.mkdtemp(prefix="livecli-test-")
os.environ["LIVECLI_CONFIG_DIR"] = os.path.join(TMP, "config")
os.environ["LIVEPROBES_DIR"] = os.path.join(TMP, "probes")

import fake_live  # noqa: E402
from livecli import cli  # noqa: E402

SONG, SRV = fake_live.serve(PORT)


def run(*args):
    buf, err = io.StringIO(), io.StringIO()
    cli.JSON = False
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
        code = cli.main(list(args))
    return code, buf.getvalue(), err.getvalue()


def run_json(*args):
    code, outp, err = run("--json", *args)
    return code, json.loads(outp) if outp.strip() else None


class LiveCommands(unittest.TestCase):
    def test_ping_and_song(self):
        self.assertEqual(run("ping")[0], 0)
        code, s = run_json("song")
        self.assertEqual(code, 0)
        self.assertEqual(s["tempo"], 120.0)
        self.assertEqual(s["signature"], "4/4")
        self.assertEqual(s["live"], "12.4.7")

    def test_tracks_and_devices(self):
        code, ts = run_json("tracks")
        self.assertEqual([t["ref"] for t in ts], ["t0", "t1", "t2", "r0", "r1", "m"])
        code, out, _ = run("devices", "bass")
        self.assertIn("[0.0.0]", out)
        self.assertIn("Glue Compressor", out)
        self.assertEqual(run("devices", "nope")[0], 1)

    def test_params_and_set_native(self):
        code, out, _ = run("set", "vox", "eq", "1 Gain A", "-3 dB")
        self.assertEqual(code, 0, out)
        self.assertIn("-3.0 dB", out)
        code, out, _ = run("set", "vox", "eq", "Filter Type", "Notch")
        self.assertIn("Notch", out)
        code, out, _ = run("set", "vox", "eq", "Frequency", "1.5 kHz")
        self.assertIn("1.50 kHz", out)

    def test_set_plugin_by_readback(self):
        code, out, _ = run("set", "vox", "soothe2", "depth", "4")
        self.assertEqual(code, 0, out)
        self.assertIn("-> 4.0", out)

    def test_on_off_and_mode(self):
        cfg = os.environ["LIVECLI_CONFIG_DIR"]
        os.makedirs(cfg, exist_ok=True)
        with open(os.path.join(cfg, "config.json"), "w") as fh:
            json.dump({"modes": {"recording": ["soothe2"]}}, fh)
        self.assertEqual(run("off", "vox", "soothe2")[0], 0)
        self.assertEqual(SONG.tracks[0].devices[1].parameters[0].value, 0)
        run("on", "vox", "soothe2")
        code, out, _ = run("mode", "recording")
        self.assertIn("1 devices off", out)
        self.assertEqual(SONG.tracks[0].devices[1].parameters[0].value, 0)
        run("mode", "restore")
        self.assertEqual(SONG.tracks[0].devices[1].parameters[0].value, 1)

    def test_mixer(self):
        self.assertEqual(run("volume", "bass", "-6")[0], 0)
        self.assertIn("-6.0 dB", str(SONG.tracks[1].mixer_device.volume))
        self.assertEqual(run("send", "bass", "A", "-12")[0], 0)
        self.assertIn("-12.0", str(SONG.tracks[1].mixer_device.sends[0]))

    def test_routing(self):
        code, r = run_json("routing", "vox", "Ext. Out", "3/4")
        self.assertEqual(r["after"], ["Ext. Out", "3/4"])
        self.assertEqual(run("routing", "vox", "Nowhere")[0], 1)

    def test_transport_and_tempo(self):
        run("tempo", "98")
        self.assertEqual(SONG.tempo, 98.0)
        run("signature", "7/8")
        run("jump", "3")
        self.assertAlmostEqual(SONG.current_song_time, 7.0)  # 2 bars of 7/8 = 7 beats
        run("signature", "4/4")
        run("play")
        self.assertTrue(SONG.is_playing)
        run("stop")
        run("loop", "on", "5", "4")
        self.assertEqual((SONG.loop, SONG.loop_start, SONG.loop_length), (True, 16.0, 16.0))

    def test_locators(self):
        code, out, _ = run("locator", "9", "Drop")
        self.assertEqual(code, 0, out)
        self.assertTrue(any(c.name == "Drop" and c.time == 32.0 for c in SONG.cue_points))
        code, ls = run_json("locators")
        self.assertIn("Drop", [l["name"] for l in ls])

    def test_tracks_create_delete(self):
        n = len(SONG.tracks)
        code, r = run_json("track-new", "midi", "Keys")
        self.assertEqual(r["name"], "Keys")
        self.assertEqual(len(SONG.tracks), n + 1)
        run("track-delete", "keys")
        self.assertEqual(len(SONG.tracks), n)

    def test_clips_and_notes(self):
        self.assertEqual(run("clip", "new", "synth", "0", "8")[0], 0)
        notes = [{"pitch": 60, "start": 0, "duration": 1, "velocity": 100}, {"pitch": 64, "start": 1, "duration": 1}]
        sys_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps(notes))
        try:
            code, r = run_json("set-notes", "synth", "0")
        finally:
            sys.stdin = sys_stdin
        self.assertEqual(r["notes"], 2)
        code, out, _ = run("notes", "synth", "0")
        self.assertEqual(len(json.loads(out)), 2)
        code, cl = run_json("clips")
        self.assertEqual(cl[0]["length"], 8.0)

    def test_load_replace(self):
        code, r = run_json("search", "utility", "audio_effects")
        self.assertEqual(r, ["audio_effects/Utility"])
        n = len(SONG.master_track.devices)
        run("load", "m", "audio_effects/Utility")
        self.assertEqual(len(SONG.master_track.devices), n + 1)
        code, out, _ = run("replace", "m", "utility", "audio_effects/Utility")
        self.assertEqual(code, 0, out)
        self.assertEqual(len(SONG.master_track.devices), n + 1)
        run("delete", "m", "utility")
        self.assertEqual(len(SONG.master_track.devices), n)

    def test_unknown_command(self):
        self.assertEqual(run("frobnicate")[0], 2)

    def test_help_lists_groups(self):
        code, out, _ = run("help")
        for g in ("Transport", "Probes", "Scenes and clips"):
            self.assertIn(g, out)


class Probes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = subprocess.Popen([sys.executable, "-I", os.path.join(ROOT, "probes", "mock", "mock_probe.py")],
                                    env=dict(os.environ), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(4)   # a few seconds of history

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()

    def test_list(self):
        code, r = run_json("probes")
        self.assertEqual(code, 0)
        self.assertEqual(len(r["probes"]), 4)
        self.assertEqual(r["servers"][0]["protocol"], "0.1")

    def test_measure(self):
        code, r = run_json("measure", "#3", "1")
        self.assertEqual(code, 0)
        self.assertIn("lufs_s", r["measure"])
        self.assertEqual(len(r["measure"]["bands_mid"]), 31)

    def test_compare_finds_the_bell(self):
        for extra in ([], ["--live"]):
            code, r = run_json("compare", "pre", "post", "2", *extra)
            self.assertEqual(code, 0)
            g = r["gain_db"]
            k1k = min(range(31), key=lambda k: abs(r["centers"][k] - 1000))
            base = sorted(x for x in g if x is not None)[len([x for x in g if x is not None]) // 2]
            self.assertAlmostEqual(g[k1k] - base, 6.0, delta=0.6)
            self.assertAlmostEqual(base, -2.0, delta=0.6)

    def test_mix(self):
        code, r = run_json("mix", "1")
        self.assertIn("low-mid", r["crowded"])

    def test_label_and_find(self):
        run("probe-label", "#3", "bass di")
        code, r = run_json("measure", "bass di", "0.2")
        self.assertEqual(r["probe"]["probe_id"], 3)
        self.assertEqual(run("measure", "vox", "0.2")[0], 1)   # ambiguous: two probes on Vox


if __name__ == "__main__":
    unittest.main()
