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
P_CENTERS = [20.0 * 2 ** (k / 3.0) for k in range(31)]
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
        self.assertEqual(run("devices", "nope")[0], 4)        # not_found

    def test_params_and_set_native(self):
        code, out, _ = run("set", "vox", "eq", "1 Gain A", "-3 dB")
        self.assertEqual(code, 0, out)
        self.assertIn("-3.0 dB", out)
        code, out, _ = run("set", "vox", "eq", "Filter Type", "Notch")
        self.assertIn("Notch", out)
        code, out, _ = run("set", "vox", "eq", "Frequency", "1.5 kHz")
        self.assertIn("1.50 kHz", out)

    def test_set_time_units(self):
        code, out_, _ = run("set", "synth", "drift", "Env 1 Decay", "30 ms")
        self.assertIn("-> 30 ms", out_)
        code, out_, _ = run("set", "synth", "drift", "Env 1 Decay", "1.5 s")
        self.assertIn("-> 1.50 s", out_)

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

    def test_pan_and_record(self):
        self.assertEqual(run("pan", "bass", "20L")[0], 0)
        self.assertAlmostEqual(SONG.tracks[1].mixer_device.panning.value, -0.4)
        self.assertEqual(str(SONG.tracks[1].mixer_device.panning), "20L")
        run("pan", "bass", "C")
        self.assertEqual(SONG.tracks[1].mixer_device.panning.value, 0.0)
        self.assertEqual(run("pan", "bass", "20")[0], 1)
        SONG.record_mode = False
        run("record")
        self.assertTrue(SONG.record_mode)
        run("record", "off")
        self.assertFalse(SONG.record_mode)

    def test_probe_add_without_probe_server(self):
        # the plugin loads and gets its ID, then the CLI waits for it on a socket: here there is none
        n = len(SONG.tracks[2].devices)
        code, out, err = run("probe-add", "synth", "first")
        self.assertEqual(code, 6)                             # probe error
        self.assertIn("does not report", err)
        d = SONG.tracks[2].devices[0]
        self.assertEqual(d.name, "Loudline Probe")
        self.assertGreater(d.parameters[1].value, 0)
        del SONG.tracks[2].devices[0]
        self.assertEqual(len(SONG.tracks[2].devices), n)

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

    def test_scene_fire(self):
        SONG.scenes[1].is_triggered = False
        self.assertEqual(run("scene", "fire", "1")[0], 0)
        self.assertTrue(SONG.scenes[1].is_triggered)
        code, sc = run_json("scenes")
        self.assertEqual(len(sc), 2)

    def test_ab_impact_inspect(self):
        code, r = run_json("ab", "vox", "eq", "b")
        self.assertEqual(r["using"], "B")
        run("ab", "vox", "eq", "a")
        code, rows = run_json("impact", "3")
        self.assertEqual(len(rows), 3)
        code, r = run_json("inspect", "device", "vox", "eq")
        self.assertEqual(r["type"], "Device")
        self.assertIn("parameters", r["properties"])

    def test_replace_is_one_undo_step(self):
        n = getattr(SONG, "undo_steps", 0)
        run("load", "m", "audio_effects/Utility")
        run("replace", "m", "utility", "audio_effects/Utility")
        self.assertEqual(SONG.undo_steps, n + 1)
        run("delete", "m", "utility")

    def test_insert_duplicate_rack_chain(self):
        n = len(SONG.tracks[1].devices)
        self.assertEqual(run("insert", "bass", "Utility")[0], 0)
        self.assertEqual(run("duplicate", "bass", "utility")[0], 0)
        self.assertEqual(len(SONG.tracks[1].devices), n + 2)
        del SONG.tracks[1].devices[n:]
        code, r = run_json("rack", "bass", "glue rack")
        self.assertEqual(r["chains"][0]["devices"], ["Glue Compressor"])
        code, r = run_json("chain", "bass", "0.0", "volume=-6", "pan=20L", "mute=on")
        self.assertEqual((r["volume"], r["pan"], r["mute"]), ("-6.0 dB", "20L", True))
        run("chain", "bass", "0.0", "mute=off")

    def test_clip_props_and_envelope(self):
        run("clip", "new", "synth", "1", "8")
        code, r = run_json("clip-set", "synth", "1", "launch_mode=gate", "launch_quantization=1/4", "legato=on")
        self.assertEqual((r["launch_mode"], r["launch_quantization"], r["legato"]), ("gate", "1/4", True))
        code, r = run_json("envelope-set", "synth", "1", "mixer", "volume", "0:4=-6dB", "4:4=0dB")
        self.assertEqual(r["steps"], 2)
        code, r = run_json("envelope", "synth", "1", "mixer", "volume", "3")
        self.assertTrue(r["exists"])
        self.assertEqual(r["points"][0]["text"], "-6.0 dB")
        run("clip", "delete", "synth", "1")

    def test_snapshot_diff_restore(self):
        run("snapshot", "t1")
        run("volume", "bass", "-12")
        code, d = run_json("diff", "t1")
        self.assertEqual(len(d["changed"]), 1)
        run("restore", "t1")
        code, d = run_json("diff", "t1")
        self.assertEqual(d["changed"], [])

    def test_diff_survives_device_moves(self):
        run("snapshot", "mv")
        run("set", "vox", "soothe2", "depth", "=0.75")
        run("insert", "vox", "Utility", "0")                # every device moves one position right
        code, d = run_json("diff", "mv")
        self.assertEqual([c[1] for c in d["changed"]], ["Vox Main / soothe2 / depth"])
        self.assertTrue(all("Utility" in x[1] for x in d["added"]))
        self.assertEqual(d["removed"], [])
        code, r = run_json("restore", "mv")
        self.assertEqual(r["set"], 1)
        self.assertAlmostEqual(SONG.tracks[0].devices[2].parameters[1].value, 0.5)
        del SONG.tracks[0].devices[0]

    def test_plugins_inventory(self):
        code, r = run_json("plugins", "--refresh")
        names = {(p["vendor"], p["name"]): p for p in r["plugins"]}
        self.assertIn(("Loudline", "Loudline Probe"), names)
        self.assertEqual(names[("Loudline", "Loudline Probe")]["category"], "analyzer")
        self.assertEqual(names[("Live", "Utility")]["category"], "stereo")

    def test_bounce_restores_state(self):
        SONG.stop_playing()
        SONG.loop, SONG.punch_in, SONG.punch_out = False, False, False
        n = len(SONG.tracks)
        code, r = run_json("bounce", "bass", "2", "2")
        self.assertEqual(code, 0)
        self.assertEqual(len(SONG.tracks), n + 1)
        self.assertEqual(SONG.tracks[-1].input_routing_type.display_name, "Bass")
        self.assertEqual((SONG.loop, SONG.punch_in, SONG.punch_out, SONG.is_playing, SONG.record_mode),
                         (False, False, False, False, False))
        self.assertFalse(SONG.tracks[-1].arm)
        run("track-delete", r["track"])

    def test_bounce_refuses_with_implicit_arm(self):
        SONG.stop_playing()
        SONG.tracks[2].implicit_arm = True            # a selected MIDI track, auto-armed by Live
        n = len(SONG.tracks)
        code, out_, err = run("bounce", "bass", "2", "1")
        SONG.tracks[2].implicit_arm = False
        self.assertEqual(code, 1)
        self.assertIn("still armed", err)
        self.assertFalse(SONG.record_mode)
        self.assertEqual(len(SONG.tracks), n)          # the empty bounce track was removed

    def test_clip_set_all(self):
        run("clip", "new", "synth", "2", "4")
        run("clip", "new", "synth", "3", "4")
        code, r = run_json("clip-set-all", "synth", "--session", "legato=on")
        self.assertGreaterEqual(r["changed"], 2)
        self.assertTrue(SONG.tracks[2].clip_slots[2].clip.legato and SONG.tracks[2].clip_slots[3].clip.legato)
        code, r = run_json("clip-set-all", "all", "--audio", "warping=off")
        audio = [c for t in SONG.tracks for c in t.arrangement_clips if c.is_audio_clip]
        self.assertEqual(r["changed"], len(audio))
        self.assertTrue(all(c.warping is False for c in audio))
        run("clip", "delete", "synth", "2")
        run("clip", "delete", "synth", "3")

    def test_error_codes_and_hints(self):
        code, r = run_json("devices", "nope")
        self.assertEqual(code, 4)
        self.assertEqual(r["code"], "not_found")
        os.environ["LIVECLI_PORT"] = str(free_port())          # nobody listens there
        try:
            code, r = run_json("ping")
        finally:
            os.environ["LIVECLI_PORT"] = str(PORT)
        self.assertEqual(code, 3)
        self.assertEqual(r["code"], "live_not_running")
        self.assertIn("live setup", r["hint"])

    def test_setup_doctor_skill_config(self):
        lib = os.path.join(TMP, "userlib")
        code, r = run_json("setup", "--user-library", lib)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.exists(os.path.join(lib, "Remote Scripts", "LiveCLI", "commands.py")))
        code, r = run_json("doctor")
        self.assertTrue(any(c["check"] == "LiveCLI answering on port %d" % PORT and c["ok"] for c in r["checks"]))
        code, out_, _ = run("skill", "install", "--dir", os.path.join(TMP, "skill"))
        self.assertEqual(code, 0)
        text = open(os.path.join(TMP, "skill", "SKILL.md")).read()
        self.assertIn("name: livecli", text)
        self.assertIn("live compare", text)
        run("config", "set", "probe_plugin", '"Reference Probe"')
        code, out_, _ = run("config", "show")
        self.assertIn("Reference Probe", out_)
        self.assertEqual(run("wait", "2")[0], 0)
        code, out_, _ = run("completion", "zsh")
        self.assertIn("compdef _live live", out_)

    def test_midi_tools(self):
        run("clip", "new", "synth", "2", "4")
        self.assertEqual(run("pattern", "synth", "2", "x...X...x.x.x...", "pitch=36")[0], 0)
        n = json.loads(run("notes", "synth", "2")[1])
        self.assertEqual([x["start"] for x in n], [0.0, 1.0, 2.0, 2.5, 3.0])
        self.assertEqual(n[1]["velocity"], 127.0)
        run("euclid", "synth", "2", "3", "8", "step=1/8", "pitch=38")
        n = json.loads(run("notes", "synth", "2")[1])
        self.assertEqual(len(n), 3)
        run("notes-edit", "synth", "2", "ratchet", "n=2")
        self.assertEqual(len(json.loads(run("notes", "synth", "2")[1])), 6)
        sys_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps([{"pitch": 60, "start": 0, "duration": 1}, {"pitch": 64, "start": 0, "duration": 1},
                                            {"pitch": 67, "start": 0, "duration": 1}]))
        try:
            run("set-notes", "synth", "2")
        finally:
            sys.stdin = sys_stdin
        run("notes-edit", "synth", "2", "arp", "mode=up", "rate=1/8")
        n = sorted(json.loads(run("notes", "synth", "2")[1]), key=lambda x: x["start"])
        self.assertEqual([x["pitch"] for x in n], [60, 64])          # 1 beat at 1/8 = two steps
        run("notes-edit", "synth", "2", "scale", "steps=2")          # C major: C -> E, E -> G
        n = sorted(json.loads(run("notes", "synth", "2")[1]), key=lambda x: x["start"])
        self.assertEqual([x["pitch"] for x in n], [64, 67])
        self.assertEqual(run("notes-edit", "synth", "2", "humanize", "timing=0.01")[0], 0)
        run("clip", "delete", "synth", "2")

    def test_transpose_in_scale(self):
        from livecli import cmd_midi as M
        major = [0, 2, 4, 5, 7, 9, 11]
        self.assertEqual(M.transpose_in_scale(60, 1, 0, major), 62)
        self.assertEqual(M.transpose_in_scale(71, 1, 0, major), 72)
        self.assertEqual(M.transpose_in_scale(60, -1, 0, major), 59)
        self.assertEqual(M.euclid(3, 8), [1, 0, 0, 1, 0, 0, 1, 0])

    def test_als_lint_and_diff(self):
        import gzip as _gz

        def ext(name, out_ch, in_ch, on=True):
            return ('<ProxyAudioEffectDevice><UserName Value="%s"/><On><Manual Value="%s"/></On>'
                    '<OutputHelper><UpperDisplayString Value="Ext. Out"/><LowerDisplayString Value="%s"/></OutputHelper>'
                    '<InputHelper><UpperDisplayString Value="Ext. In"/><LowerDisplayString Value="%s"/></InputHelper>'
                    '</ProxyAudioEffectDevice>' % (name, "true" if on else "false", out_ch, in_ch))

        def track(name, devs, tag="AudioTrack", sample=None):
            smp = '<Path Value="%s"/>' % sample if sample else ""
            return ('<%s><Name><EffectiveName Value="%s"/></Name><DeviceChain><AudioOutputRouting><Target Value="x"/>'
                    '<UpperDisplayString Value="Main"/><LowerDisplayString Value=""/></AudioOutputRouting>'
                    '<DeviceChain><Devices>%s</Devices></DeviceChain></DeviceChain>%s</%s>' % (tag, name, devs, smp, tag))

        def als(path, tracks, tempo=120):
            xml = ('<Ableton Creator="Ableton Live 12.4.16"><LiveSet><Tracks>%s</Tracks><MainTrack><Name><EffectiveName Value="Main"/>'
                   '</Name><DeviceChain><Mixer><Tempo><Manual Value="%s"/></Tempo></Mixer><DeviceChain><Devices/></DeviceChain>'
                   '</DeviceChain></MainTrack></LiveSet></Ableton>' % ("".join(tracks), tempo))
            with _gz.open(path, "wb") as fh:
                fh.write(xml.encode())

        a_ = os.path.join(TMP, "a.als")
        b_ = os.path.join(TMP, "b.als")
        als(a_, [track("Bass", ext("Fatso", "11/12", "11/12")), track("Vox", ext("Neve", "11/12", "17/18"), sample="/nope/vox.wav")])
        als(b_, [track("Bass", ext("Fatso", "11/12", "11/12", on=False)), track("Keys", "")], tempo=124)
        code, r = run_json("als-lint", a_)
        msgs = [i["issue"] for i in r["issues"]]
        self.assertTrue(any("hardware" in i["name"] and "Fatso" in i["issue"] and "Neve" in i["issue"] for i in r["issues"]))
        self.assertTrue(any("missing sample" in m for m in msgs))
        code, d = run_json("als-diff", a_, b_)
        whats = {(x["what"], x["track"]) for x in d}
        self.assertIn(("tempo", ""), whats)
        self.assertIn(("track removed", "Vox"), whats)
        self.assertIn(("track added", "Keys"), whats)
        self.assertIn(("device on/off", "Bass"), whats)

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

    def test_sync_probes(self):
        code, r = run_json("sync", "--probes", "post", "pre", "1")
        self.assertEqual(code, 0)
        self.assertAlmostEqual(r["late_ms"], 10.0, delta=0.3)
        self.assertAlmostEqual(r["track_delay_ms"], -10.0, delta=0.3)

    def test_refcompare_and_explain_math(self):
        code, r = run_json("refcompare", "post", "pre", "1")
        self.assertEqual(code, 0)
        self.assertTrue(any(reg["db"] > 5 and 700 < reg["at"] < 1400 for reg in r["regions"]))   # the +6 dB bell

    def test_label_and_find(self):
        run("probe-label", "#3", "bass di")
        code, r = run_json("measure", "bass di", "0.2")
        self.assertEqual(r["probe"]["probe_id"], 3)
        self.assertEqual(run("measure", "vox", "0.2")[0], 4)   # ambiguous: two probes on Vox


if __name__ == "__main__":
    unittest.main()
