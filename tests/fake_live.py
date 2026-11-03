"""A tiny fake of Ableton Live's Python API, enough to run remote_script/LiveCLI/commands.py outside Live,
plus a TCP server that behaves like the Remote Script. Used by the tests."""
import json
import os
import socket
import sys
import threading
import types


class Param:
    def __init__(self, name, value=0.0, lo=0.0, hi=1.0, quantized=False, fmt=None, choices=None):
        self.name, self.value, self.min, self.max = name, value, lo, hi
        self.is_quantized, self.is_enabled = quantized, True
        self.fmt, self.choices = fmt, choices

    def str_for_value(self, v):
        if self.choices:
            return self.choices[int(v)]
        if self.fmt:
            return self.fmt(v)
        return "%.2f" % v

    def __str__(self):
        return self.str_for_value(self.value)


def db_fmt(v):           # 0..1 -> -70..+6 dB
    return "%.1f dB" % (-70 + 76 * v)


def hz_fmt(v):           # 0..1 -> 10 Hz..22 kHz (log)
    f = 10 * (2200 ** v)
    return ("%.2f kHz" % (f / 1000)) if f >= 1000 else ("%.0f Hz" % f)


class Device:
    def __init__(self, name, cls="Eq8", params=None, chains=None):
        self.name, self.class_name, self.type = name, cls, 2
        self.parameters = [Param("Device On", 1.0, 0, 1, True, choices=["Off", "On"])] + (params or [])
        self.chains = chains or []
        self.can_have_chains = bool(chains)


class Chain:
    def __init__(self, name, devices):
        self.name, self.devices = name, devices

    def delete_device(self, i):
        del self.devices[i]


class Routing:
    def __init__(self, n):
        self.display_name = n


class MidiNote:
    def __init__(self, pitch, start_time, duration, velocity, mute):
        self.pitch, self.start_time, self.duration, self.velocity, self.mute = pitch, start_time, duration, velocity, mute
        self.probability = 1.0


class Clip:
    def __init__(self, length, midi=True, name=""):
        self.name, self.color, self.length, self.looping = name, 0xFF0000, length, True
        self.loop_start, self.loop_end = 0.0, length
        self.is_midi_clip, self.is_audio_clip = midi, not midi
        self.is_playing = self.is_triggered = self.muted = False
        self.start_time, self.end_time = 0.0, length
        self.notes = []

    def get_notes_extended(self, *a):
        return list(self.notes)

    def remove_notes_extended(self, *a):
        self.notes = []

    def add_new_notes(self, specs):
        self.notes += [MidiNote(s.pitch, s.start_time, s.duration, s.velocity, s.mute) for s in specs]


class ClipSlot:
    def __init__(self):
        self.clip = None

    @property
    def has_clip(self):
        return self.clip is not None

    def create_clip(self, length):
        self.clip = Clip(length)

    def delete_clip(self):
        self.clip = None

    def fire(self):
        if self.clip:
            self.clip.is_playing = True

    def stop(self):
        if self.clip:
            self.clip.is_playing = False


class Mixer:
    def __init__(self, n_sends):
        self.volume = Param("Track Volume", 0.85, 0, 1, fmt=db_fmt)
        self.panning = Param("Track Panning", 0.0, -1, 1, fmt=lambda v: "C" if abs(v) < 0.01 else
                             ("%dL" % round(-v * 50) if v < 0 else "%dR" % round(v * 50)))
        self.sends = [Param("Send %d" % i, 0.0, 0, 1, fmt=db_fmt) for i in range(n_sends)]


class Track:
    def __init__(self, name, devices=None, midi=False, n_sends=2, n_slots=4):
        self.name, self.color, self.devices = name, 0x3377FF, devices or []
        self.mixer_device = Mixer(n_sends)
        self.mute = self.solo = self.arm = False
        self.can_be_armed, self.has_midi_input, self.is_foldable, self.is_grouped = True, midi, False, False
        self.current_monitoring_state, self.is_frozen, self.fold_state = 1, False, False
        self.group_track = None
        self.output_meter_left = self.output_meter_right = 0.5
        self.available_output_routing_types = [Routing("Master"), Routing("Ext. Out")]
        self.available_output_routing_channels = [Routing("1/2"), Routing("3/4")]
        self.available_input_routing_types = [Routing("Ext. In"), Routing("No Input")]
        self.available_input_routing_channels = [Routing("1"), Routing("2")]
        self.output_routing_type, self.output_routing_channel = self.available_output_routing_types[0], self.available_output_routing_channels[0]
        self.input_routing_type, self.input_routing_channel = self.available_input_routing_types[0], self.available_input_routing_channels[0]
        self.clip_slots = [ClipSlot() for _ in range(n_slots)]
        self.arrangement_clips = [Clip(16.0, midi, "intro")]

    def delete_device(self, i):
        del self.devices[i]

    def stop_all_clips(self):
        for s in self.clip_slots:
            s.stop()

    def duplicate_clip_slot(self, i):
        self.clip_slots[i + 1].clip = self.clip_slots[i].clip
        return i + 1


class Scene:
    def __init__(self, name):
        self.name, self.color, self.tempo, self.is_triggered = name, 0, 0.0, False

    def fire(self):
        self.is_triggered = True


class Cue:
    def __init__(self, time, name):
        self.time, self.name = time, name


class View:
    def __init__(self):
        self.selected_track = self.selected_scene = self.selected_device = None

    def select_device(self, d):
        self.selected_device = d


class Song:
    def __init__(self):
        self.tempo, self.signature_numerator, self.signature_denominator = 120.0, 4, 4
        self.is_playing, self.current_song_time = False, 0.0
        self.loop, self.loop_start, self.loop_length = False, 0.0, 16.0
        self.metronome = self.record_mode = self.session_record = self.arrangement_overdub = False
        self.punch_in = self.punch_out = False
        self.can_undo, self.can_redo = True, False
        self.root_note, self.scale_name = 0, "Major"
        eq = Device("EQ Eight", "Eq8", [Param("1 Frequency A", 0.5, fmt=hz_fmt), Param("1 Gain A", 0.5, fmt=lambda v: "%.1f dB" % (-15 + 30 * v)),
                                        Param("1 Filter Type A", 3, 0, 7, True, choices=["48", "12", "LS", "Bell", "Notch", "HS", "12h", "48h"])])
        plugin = Device("soothe2", "AuPluginDevice", [Param("depth", 0.5, fmt=lambda v: "%.1f" % (v * 20))])
        rack = Device("Glue Rack", "AudioEffectGroupDevice", chains=[Chain("Chain", [Device("Glue Compressor", "GlueCompressor")])])
        self.tracks = [Track("Vox Main", [eq, plugin]), Track("Bass", [rack]), Track("Synth", [], midi=True)]
        self.return_tracks = [Track("A-Reverb", [Device("Reverb", "Reverb")]), Track("B-Delay")]
        self.master_track = Track("Main", [Device("Limiter", "Limiter")])
        self.scenes = [Scene("Intro"), Scene("Drop")]
        self.cue_points = [Cue(0.0, "Start")]
        self.view = View()

    # transport
    def start_playing(self):
        self.is_playing = True

    def continue_playing(self):
        self.is_playing = True

    def stop_playing(self):
        self.is_playing = False

    def stop_all_clips(self):
        pass

    def undo(self):
        self.can_redo = True

    def redo(self):
        pass

    def tap_tempo(self):
        pass

    def capture_midi(self):
        pass

    def set_or_delete_cue(self):
        hit = [c for c in self.cue_points if abs(c.time - self.current_song_time) < 0.01]
        if hit:
            self.cue_points.remove(hit[0])
        else:
            self.cue_points.append(Cue(self.current_song_time, ""))
            self.cue_points.sort(key=lambda c: c.time)

    # tracks
    def create_audio_track(self, i):
        t = Track("Audio")
        self.tracks.insert(i if i >= 0 else len(self.tracks), t)

    def create_midi_track(self, i):
        t = Track("MIDI", midi=True)
        self.tracks.insert(i if i >= 0 else len(self.tracks), t)

    def create_return_track(self):
        self.return_tracks.append(Track("Return"))

    def delete_track(self, i):
        del self.tracks[i]

    def delete_return_track(self, i):
        del self.return_tracks[i]

    def duplicate_track(self, i):
        import copy
        self.tracks.insert(i + 1, copy.copy(self.tracks[i]))

    def create_scene(self, i):
        self.scenes.insert(i if i >= 0 else len(self.scenes), Scene(""))

    def delete_scene(self, i):
        del self.scenes[i]

    def duplicate_scene(self, i):
        self.scenes.insert(i + 1, Scene(self.scenes[i].name))

    def move_device(self, d, cont, pos):
        cont.devices.remove(d)
        cont.devices.insert(pos, d)


class BrowserItem:
    def __init__(self, name, children=(), loadable=False, device=None):
        self.name, self.children, self.is_loadable = name, list(children), loadable
        self.is_folder = bool(children)
        self.device = device


class Browser:
    def __init__(self, song):
        self.song = song
        self.plugins = BrowserItem("Plug-Ins", [BrowserItem("VST3", [BrowserItem("Loudline", [
            BrowserItem("Loudline Probe", loadable=True, device=lambda: Device(
                "Loudline Probe", "PluginDevice", [Param("Probe ID", 0.0, 0, 1, fmt=lambda v: "%d" % round(v * 999))]))])])])
        self.audio_effects = BrowserItem("Audio Effects", [BrowserItem("Utility", loadable=True,
                                                                       device=lambda: Device("Utility", "StereoGain"))])
        empty = BrowserItem("x")
        self.user_library = self.instruments = self.midi_effects = self.max_for_live = empty
        self.drums = self.sounds = self.samples = self.clips = self.packs = self.current_project = empty

    def load_item(self, it):
        t = self.song.view.selected_track
        d = self.song.view.selected_device
        pos = t.devices.index(d) + 1 if d in t.devices else len(t.devices)
        t.devices.insert(pos, it.device())


class Application:
    def __init__(self, song):
        self.browser = Browser(song)
        self.average_process_usage, self.peak_process_usage = 23.0, 41.0

    def get_major_version(self):
        return 12

    def get_minor_version(self):
        return 4

    def get_bugfix_version(self):
        return 7


class MidiNoteSpecification:
    def __init__(self, pitch, start_time, duration, velocity, mute=False):
        self.pitch, self.start_time, self.duration, self.velocity, self.mute = pitch, start_time, duration, velocity, mute


def install():
    """Create the fake `Live` module and return (commands module, fake control surface)."""
    song = Song()
    app = Application(song)
    live = types.ModuleType("Live")
    live.Application = types.SimpleNamespace(get_application=lambda: app)
    live.Clip = types.SimpleNamespace(MidiNoteSpecification=MidiNoteSpecification)
    sys.modules["Live"] = live
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, os.path.join(here, "..", "remote_script", "LiveCLI"))
    import commands
    cs = types.SimpleNamespace(_song=song, schedule=lambda ticks, fn: fn())
    return commands, cs


def serve(port):
    """Run the fake Remote Script on 127.0.0.1:port in a thread. Returns (song, server socket)."""
    import queue
    commands, cs = install()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", port))
    srv.listen(8)
    lock = threading.Lock()

    def client(c):
        data = b""
        while not data.endswith(b"\n"):
            chunk = c.recv(65536)
            if not chunk:
                break
            data += chunk
        req = json.loads(data.decode() or "{}")
        box = queue.Queue()
        with lock:
            try:
                res = commands.run(cs, req, box)
                res = box.get() if res is commands.DEFERRED else {"ok": True, "result": res}
            except Exception as e:
                res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        c.sendall((json.dumps(res) + "\n").encode())
        c.close()

    def loop():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                break
            threading.Thread(target=client, args=(c,), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()
    return cs._song, srv
