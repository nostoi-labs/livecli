# LiveCLI

A command line for **Ableton Live**, designed to be driven by **AI agents** (Claude Code and similar) and by humans.

- **No MCP server, no daemon:** a small Remote Script inside Live plus a `live` command. Each call is one short JSON line on localhost. The output is compact text, or JSON with `--json`, which keeps agent token usage low.
- **Sees the whole set:** tracks, **return tracks and master**, devices inside racks, plugin parameters, routing (including External Audio Effect hardware inserts), clips, scenes, notes, locators, transport.
- **Changes things safely:** set parameters by their display value (`-3 dB`, `250 Hz`, `Bell`), even on AU/VST plugins (set, read back, binary search). Replace devices without feedback loops through hardware. Use undo.
- **Listens:** the **Live Probe Protocol** ([docs/PROTOCOL.md](docs/PROTOCOL.md)) lets probe plugins on any track publish real-time measurements: LUFS, true peak, 1/3-octave bands, stereo correlation. `live compare` tells you what a plugin, or an outboard insert, really does to the signal. `live mix` tells you which tracks crowd the same frequencies.

```
$ live tracks
t0   Vox Main                -> Master                EQ Eight, soothe2
t1   Bass                    -> Master                Glue Rack
r0   A-Reverb                -> Master                Reverb
m    Main                                             Limiter

$ live set vox eq "1 Gain A" -3dB
Vox Main / EQ Eight / 1 Gain A: 0.0 dB -> -3.0 dB

$ live probe-add vox first pre
probe #1 on Vox Main (device 0) [pre]
$ live probe-add vox after soothe2 post
probe #2 on Vox Main (device 3) [post]
$ live compare pre post 10
#1 Vox Main [pre] -> #2 Vox Main [post], 10s (server)
  broadband -0.4 dB (median of the bands)
  cut   -4.2 dB at 3.2k Hz (2.5k-5.1k Hz)
```

## Install

Requirements: macOS, Ableton Live 11.3 or later (tested on Live 12.4), Python 3.9+ (standard library only).

1. Copy `remote_script/LiveCLI` to `~/Music/Ableton/User Library/Remote Scripts/`.
2. In Live, open **Settings > Link, Tempo & MIDI**. In a free **Control Surface** slot choose **LiveCLI**, with Input and Output set to None.
3. Put `live` on your PATH, e.g. `ln -s "$PWD/live" ~/.local/bin/live`.
4. Check: `live ping` should print `pong`.

Live loads Remote Scripts at startup. After you change `commands.py`, run `live reload`: no restart is needed. After you change `__init__.py`, restart Live.

## Commands

`live help` lists everything; `live help <command>` shows one command. The groups are:

- **Info:** `ping`, `song`, `cpu`, `impact` (CPU impact per track), `tracks`, `devices`, `params`, `mixer`, `meters`, `routing`, `input`, `locators`, `scenes`, `clips`, `arrangement`, `notes`
- **Transport:** `play`, `stop`, `continue`, `stop-clips`, `record`, `undo`, `redo`, `jump <bar>`, `tempo`, `signature`, `metronome`, `loop`, `tap`, `capture-midi`, `locator`, `locator-delete`
- **Tracks and mixer:** `track-new`, `track-delete`, `track-duplicate`, `track-rename`, `track-color`, `solo`, `mute`, `arm`, `monitor`, `volume`, `pan`, `send`
- **Devices:** `set`, `on`, `off`, `ab` (Live 12 A/B compare), `load`, `delete`, `move`, `rename`, `replace`, `mode`. `devices` also shows the latency Live assumes for each device.
- **Scenes and clips:** `scene ...`, `clip ...`, `set-notes`
- **Browser:** `search`, `browse`
- **Audio:** `capture` (optional, see below)
- **Probes:** `probes`, `probe-add`, `measure`, `watch`, `compare`, `mix`, `latency`, `probe-reset`, `probe-label`
- **Other:** `inspect` (what Live's API offers on the song, a track, mixer, device, parameter, clip or scene: properties with values and methods; read-only), `reload` (re-read the Remote Script commands inside Live), `call <cmd> '<json>'` (raw call to any Remote Script command), `help`.
- **Italian aliases:** `stato`, `tracce`, `parametri`, `imposta`, `accendi`/`spegni`, `carica`, `cancella`, `sposta`, `sostituisci`, `cerca`, `livelli`, `mandata`, `locatori`, `vai`, `modo`, `cattura`, `sonde`, `misura`, `confronta`, …

References are short:
- **track:** `t3`, `r0`, `m` (master), a return letter (`A`), or part of the name;
- **device:** an index, a path into racks (`2.0.1` = device 2, chain 0, device 1), or part of the name;
- **probe:** `#<probe id>`, a label, or the track name.

### Notes for agents
- Read freely. Ask the user before changing their set: there is `live undo`, but hardware and ears are real. `replace` and `mode` are grouped into a single undo step.
- Not reachable from Live's API (Live 12.4): saving, opening or exporting the set, preferences (audio device, buffer, sample rate, driver error compensation), the Delay Compensation switch, track delay, the Hardware Latency of External Audio Effect, creating group tracks. Use `inspect` to check your Live version.
- `live --json <command>` always returns JSON. Errors come back as `{"ok": false, "error": "..."}` with exit code 1.
- `live meters` reads Live's own meters (0 to 1 scale, not dBFS). For real levels use probes.

## Probes

A probe is a pass-through audio plugin that measures and publishes, following the [Live Probe Protocol](docs/PROTOCOL.md). `live` discovers every probe server under `~/Library/Application Support/LiveProbes/`.

- **Loudline Probe** (free VST3 by Nostoi, closed source, uses the [Loudline](https://fwradar.com/loudline/) engine): LUFS M/S/I, LRA, true peak, PLR, 31 bands mid/side, correlation, history, in-process compare.
- **Reference probe** (`probes/reference/`, MIT): a minimal implementation to copy when you build your own.
- **Mock probe** (`probes/mock/mock_probe.py`): a fake server with synthetic signals, for developing clients without a DAW.

Write your own probe in any language. If it speaks the protocol, `live` will use it.

`live latency <track> <device>` puts a probe before and after a device (a plugin, or an External Audio Effect with your outboard), measures the real delay on the music while it plays, and compares it with the latency Live assumes for that device. For a hardware insert it tells you how much to add to or remove from **Hardware Latency**: Live's API does not expose that setting, so you change it by hand.

`probe-add` loads the plugin set in config (`probe_plugin`, default "Loudline Probe"), gives it a free **Probe ID** through its parameter, and waits until it reports. That ID is how the CLI ties a probe to a device position in Live, even when two probes sit on the same track.

## Config

`~/.config/live-cli/config.json` (optional):

```json
{
  "port": 9878,
  "modes": {"recording": ["soothe2", "Ozone"]},
  "probe_plugin": "Loudline Probe",
  "capture": {"enabled": false, "device": "Your Audio Interface", "max_seconds": 600},
  "extensions": ["~/my-studio/live_ext.py"]
}
```

- **modes:** `live mode recording` switches off every device whose name contains one of these strings, and saves the list. `live mode restore` switches them back on. Useful to record at low latency with heavy look-ahead plugins in the set.
- **capture:** `live capture <seconds> <file.wav>` records an input device with ffmpeg launched **by Live**, so it inherits Live's microphone permission (a terminal or tmux usually lacks it). Off by default; it needs `ffmpeg` (`brew install ffmpeg`).
- **extensions:** Python files (also any `*.py` in `~/.config/live-cli/extensions/`) that add your own commands:

```python
def register(cli):
    @cli.command("hello", "hello <track>", "say hello to a track", "ext")
    def hello(a):
        t = cli.track(a[0])
        print("hello", t["name"])
```

## Develop

```
python3 -m unittest discover -s tests
```
The tests run the real `remote_script/LiveCLI/commands.py` against a fake Live API (`tests/fake_live.py`), and the probe commands against the mock probe.

## License

MIT, see [LICENSE](LICENSE). Loudline and Loudline Probe are separate products and are not covered by this license.
