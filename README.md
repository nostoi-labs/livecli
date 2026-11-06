# LiveCLI

A command line for **Ableton Live**, designed to be driven by **AI agents** (Claude Code and similar) and by humans.

- **No MCP server, no daemon:** a small Remote Script inside Live plus a `live` command. Each call is one short JSON line on localhost. The output is compact text, or JSON with `--json`, which keeps agent token usage low.
- **Sees the whole set:** tracks, **return tracks and master**, devices inside racks, plugin parameters, routing (including External Audio Effect hardware inserts), clips, scenes, notes, locators, transport.
- **Changes things safely:** set parameters by their display value (`-3 dB`, `250 Hz`, `Bell`), even on AU/VST plugins (set, read back, binary search). Replace devices without feedback loops through hardware. Use undo.
- **Measures the studio, not just the set:** level-matched A/B, gain staging in dBu with your interface calibration, phase and polarity between tracks, the measured effect of a change, a live reference comparison, the real latency of plugins and hardware inserts, Track Delay for Overbridge and MIDI-clock hardware, fingerprints of outboard gear (response, noise, THD, latency) over time, session recall with SessionRecall.
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

1. Install the `live` command, one of:
   - Homebrew: `brew install nostoi-labs/tap/livecli`
   - pipx or pip: `pipx install livecli`
   - from git: `git clone https://github.com/nostoi-labs/livecli` and `ln -s "$PWD/livecli/live" ~/.local/bin/live`
2. `live setup` copies the Remote Script into Live's User Library (`Remote Scripts/LiveCLI`).
3. In Live, open **Settings > Link, Tempo & MIDI**. In a free **Control Surface** slot choose **LiveCLI**, with Input and Output set to None.
4. Check: `live doctor`, then `live ping` should print `pong`.
5. Optional, for measurements: the probe plugins (VST3, notarized) from the [Releases](https://github.com/nostoi-labs/livecli/releases) page, or `brew install --cask nostoi-labs/tap/reference-probe` (MIT) and `nostoi-labs/tap/loudline-probe` (freeware). Install them with Live closed, then **Settings > Plug-ins > Rescan**.

Live loads Remote Scripts at startup. After updating LiveCLI run `live setup` again: changes to `commands.py` are reloaded without a restart, changes to `__init__.py` need a Live restart.

## Install with an AI agent

A user can simply tell their agent: *"I want to use LiveCLI: install it and add the skill."* This section is for that agent.

**What the agent does (terminal):**
1. Check the platform: macOS with Ableton Live 11.3 or later (12.x recommended), Python 3.9+.
2. Get LiveCLI: `brew install nostoi-labs/tap/livecli`, or `pipx install livecli`, or `git clone https://github.com/nostoi-labs/livecli` and link `live` into the PATH (`ln -s "$PWD/livecli/live" ~/.local/bin/live`).
3. `live setup`: copies the Remote Script into Live's User Library (`Remote Scripts/LiveCLI`).
4. Optional probes for measurements: install the Reference Probe or Loudline Probe package (VST3, notarized) from the Releases page.
5. `live skill install` (Claude Code; `--dir <project>/.claude/skills/livecli` for a project skill). Next session the agent knows the commands.
6. `live doctor` until every line is ✓, then `live ping`.

**What only the user can do (ask for these, don't guess):**
- In Live: **Settings > Link, Tempo & MIDI**, a free **Control Surface** slot: **LiveCLI**, Input and Output: None. If LiveCLI is not in the list, restart Live (it reads Remote Scripts at startup).
- If probes were installed: **Settings > Plug-ins > Rescan** (Live must be closed while a probe is being replaced).
- The administrator password if the probe installer asks for it, and any macOS permission prompt.

**Rules for the agent once it works:**
- Reading is free. Ask before changing the user's set; `live snapshot` before a series of changes.
- Never change Live's audio preferences (device, buffer, sample rate) or the user's projects on disk.
- When trying things, use new tracks of your own, not the user's tracks; delete them afterwards.
- Run `live doctor` first whenever a command fails; error codes and hints say what to do.

## Commands

`live help` lists everything; `live help <command>` shows one command. The groups are:

- **Info:** `ping`, `song`, `events` (pushed by Live's listeners), `cpu`, `impact` (CPU impact per track), `tracks`, `devices`, `params`, `mixer`, `meters`, `routing`, `input`, `locators`, `scenes`, `clips`, `arrangement`, `notes`, `automation` (automated and overridden parameters), `events` (what changes while it happens)
- **Transport:** `play`, `stop`, `continue`, `stop-clips`, `record`, `undo`, `redo`, `jump <bar>`, `tempo`, `signature`, `metronome`, `loop`, `tap`, `capture-midi`, `locator`, `locator-delete`, `re-enable-automation`, `back-to-arrangement`
- **Tracks and mixer:** `track-new`, `track-delete`, `track-duplicate`, `track-rename`, `track-color`, `solo`, `mute`, `arm`, `monitor`, `volume`, `pan`, `send`, `pan-mode` (stereo or split), `crossfade`, `activator`, `cue`
- **Devices:** `set`, `on`, `off`, `ab` (Live 12 A/B compare), `preset` (a plugin's presets, when it publishes them to Live, and which parameters it hides from Live), `sidechain`, `insert` (a Live device by name, no browser), `duplicate`, `load`, `delete`, `move`, `rename`, `replace`, `mode`. `devices` also shows the latency Live assumes for each device.
- **Racks:** `rack` (chains with their mixer, macros, variations, Drum Rack pads), `chain` (name, mute, solo, volume, pan of a chain), `chain-new`, `variation store|recall|delete|randomize`, `pad` (mute/solo a Drum Rack pad)
- **Scenes and clips:** `scene ...`, `clip ...`, `set-notes`, `clip-info`, `clip-set` (launch mode and quantization, legato, markers, loop, warp mode, gain, pitch...), `clip-set-all` (many clips at once, e.g. `clip-set-all all --audio warping=off` after importing files), `clip-edit` (quantize, transpose, velocity, crop, duplicate-loop), `warp-markers`, `warp-move`, `groove`, `notes-edit` (humanize, swing, ratchet, retrograde, arp, scale degrees, legato, velocity curve), `pattern` (`x...X...`), `euclid`, `arr-from-session`, `envelope` / `envelope-set` / `envelope-clear` (clip automation), `arr-add` (copy a session clip, an audio file or a new MIDI clip into the arrangement). `@N` addresses arrangement clips (`live clip delete t3 @2`).
- **Browser and plugins:** `search`, `browse`, `plugins` (inventory by category and style, see below)
- **Audio:** `bounce <track|master> <start-bar> <bars>` (records a track's output or the master into a new audio track, in real time, with Live's punch in/out: the API has no export, this is the next best thing; prints the file and where the region starts in it), `capture` (optional, see below)
- **Probes:** `probes`, `probe-add`, `measure`, `watch`, `compare`, `mix`, `latency`, `sync`, `probe-reset`, `probe-label`
- **Timing:** `grid <track> [bars] [start-bar] [--grid 1/8]` records a track (bounce) and measures where its transients fall against Live's grid; `sync` does the same live with probes (`onsets`). See *Track Delay* below.
- **Setup and reliability:** `setup` (installs the Remote Script), `doctor`, `wait`, `config`, `completion`, `skill` (a SKILL.md for agents, generated from these commands). Errors are JSON with a stable `code` and a `hint`; exit codes 3 Live not running, 4 not found or ambiguous, 5 not supported by Live's API, 6 probe.
- **Safety net:** `snapshot [name]` saves every mixer, device and chain value; `diff` shows what changed since (or between two snapshots); `restore <name> [filter]` puts values back, in one undo step; `snapshots` lists them.
- **View and offline:** `show` (bring a track, device or clip into view for the user), `view` (Session, Arranger, Detail/Clip...), `als`, `als-lint` (missing samples, hardware outputs shared by several External Audio Effects, devices off...), `als-diff` (what changed between two versions of a set). Read-only.
- **Mix tools nobody else has (with probes):** `abx` (level-matched A/B of a device: equal loudness, honest comparison), `gainstage` (peak, VU and dBu per track and into every hardware insert, with your interface calibration), `phase` (delay and polarity between two tracks), `explain` (what a set of changes did to the sound: `snapshot --measure` first), `refcompare` (your mix against a reference playing in Live, tonal balance per band), `check` (set health).
- **Hybrid studio:** `fingerprint` (frequency response, gain, noise, latency and polarity of a hardware insert or a device, with test signals from the probes, compared over time; `fingerprints` lists the history), `recall` (Live snapshot + outboard settings from SessionRecall + fingerprints).
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

## Which plugin should I use? (`live plugins`)

`live plugins` lists every plugin Live sees (from its browser, so plugins packed in shells like Waves are expanded) plus Live's own devices, grouped by category with style and emulated hardware from [livecli/catalog.json](livecli/catalog.json). With Live closed it reads the installed AU and VST3 bundles instead. The inventory is cached for a week (`--refresh` to rebuild).

```
$ live plugins la-2a
compressor (6)
  Universal Audio (UADx) / UADx LA-2A Gray Compressor  AUv2+VST3  Teletronix LA-2A leveling amplifier | opto, tube, smooth, vocals, bass
  Waves / CLA-2A                                       AUv2+VST3  Teletronix LA-2A leveling amplifier | ...
  Universal Audio (UADx) / UADx Empirical Labs Distressor     ...  note: its 'Opt' detector mode gives an opto-like (LA-2A style) curve
```

So an agent can answer "what would you put on this vocal for an LA-2A feel, and what are my options?" from the plugins the user actually owns. The catalog is a list of regex rules (category, styles, emulates, notes); anything it doesn't know shows as `uncategorized`, and the agent can still reason from the name. Add your own plugins in `~/.config/live-cli/catalog.json` (same format): your rules come before the built-in ones. Pull requests with more plugins are welcome.

## Probes

A probe is a pass-through audio plugin that measures and publishes, following the [Live Probe Protocol](docs/PROTOCOL.md). `live` discovers every probe server under `~/Library/Application Support/LiveProbes/`.

- **Loudline Probe** (free VST3 by Nostoi, closed source, uses the [Loudline](https://fwradar.com/loudline/) engine): LUFS M/S/I, LRA, true peak, PLR, 31 bands mid/side, correlation, history, in-process compare.
- **Reference probe** (`probes/reference/`, MIT): a minimal implementation to copy when you build your own.
- **Mock probe** (`probes/mock/mock_probe.py`): a fake server with synthetic signals, for developing clients without a DAW.

Write your own probe in any language. If it speaks the protocol, `live` will use it.

`live latency <track> <device>` puts a probe before and after a device (a plugin, or an External Audio Effect with your outboard), measures the real delay on the music while it plays, and compares it with the latency Live assumes for that device. For a hardware insert it tells you how much to add to or remove from **Hardware Latency**: Live's API does not expose that setting, so you change it by hand.

`probe-add` loads the plugin set in config (`probe_plugin`, default "Loudline Probe"), gives it a free **Probe ID** through its parameter, and waits until it reports. That ID is how the CLI ties a probe to a device position in Live, even when two probes sit on the same track.

## Track Delay for devices with their own sequencer

Overbridge devices and hardware synced to MIDI clock play from their own sequencer: Live compensates only the latency the plugin *declares*. If the real one differs, the track is early or late and you fix it with Track Delay (Live's API cannot set it). `live grid` measures it on the audio:

1. Calibrate once with a dry sound from the device's own sequencer (its metronome, or a kick on the grid): the latency belongs to the path (device, USB, buffers), not to the sound, so the value also holds for pads and effects.
2. `live grid <track>` gives the median offset from the grid; subtract the offset of a reference sequenced by Live (the detector's attack bias, e.g. +1.4 ms for a Drift click). The difference, with the sign flipped, is the Track Delay.
3. Re-measure after setting it.

Tested blind in Live 12.4.16: a hidden 21 ms delay on a click track measured as 21.0 ms; after Track Delay -21 ms the track was back on the reference within 0.01 ms.

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

## For agents: the skill

`live skill install` writes `SKILL.md` (generated from the commands, plus rules and recipes) to `~/.claude/skills/livecli/`, or to `--dir`, for example a project's `.claude/skills/livecli/`. Claude Code then knows the commands without reading this README. Regenerate it after updating LiveCLI.

## Develop

```
python3 -m unittest discover -s tests
```
The tests run the real `remote_script/LiveCLI/commands.py` against a fake Live API (`tests/fake_live.py`), and the probe commands against the mock probe.

## License

MIT, see [LICENSE](LICENSE). Loudline and Loudline Probe are separate products and are not covered by this license.
