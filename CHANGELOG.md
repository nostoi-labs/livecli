# Changelog

## 0.2.0 (2026-11-10)

Tested in Live 12.4.16 with Reference/Loudline Probe 0.1.4, on a studio with an Antelope Orion 32+ Gen 4, outboard (Empirical Labs Distressor and Fatso, Bricasti M7, Hologram and OTO pedals) and a Digitakt II on Overbridge.

- Packaging: `pip`/`pipx install nostoi-livecli` (the command stays `live`, the Remote Script is inside the package), Homebrew tap `nostoi-labs/tap`, `live version`.
- Hardware tests: `fingerprint` on ten outboard units (gain, latency, polarity, noise, THD+N, response); `sync` on Overbridge.
- Fixes from the hardware tests: `sync` subtracts the latency the track's devices declare (Live compensates it; an Overbridge plugin declares about 44 ms) and says that it cannot see a Track Delay once set; `grid` reads WAV/AIFF with the standard library (ffmpeg only for other formats); `bounce`/`grid` work when the start is past the end of a short set, restore the automatically armed track and the play position; commands stopped with kill or a dropped ssh session clean up like Ctrl-C (probes removed, track volume restored). README: Overbridge sync instead of Live's MIDI clock.

- Setup and reliability: `setup`, `doctor`, `wait`, `config`, `completion`, `skill`; error codes, hints, exit codes; CI.
- Devices: `preset` (plugin presets and hidden parameters), `sidechain`.
- MIDI: `notes-edit` (humanize, swing, ratchet, retrograde, arp, scale degrees, legato, velocity curve), `pattern`, `euclid`; `groove`, `warp-move`, `arr-from-session`.
- Offline: `als-lint`, `als-diff`. `docs/API-GAPS.md` for agents.
- `events` pushed by Live listeners (song, tracks, mixer, every device parameter).
- New: `abx` (level-matched A/B), `gainstage` (VU and dBu, hardware inputs), `phase`, `explain` (+ `snapshot --measure`), `refcompare`, `check`, `fingerprint`, `recall` (SessionRecall).
- Fixes: snapshot diff/restore recognise devices by name and order, not position (a device insertion no longer looks like removals); Utility parameter names of Live 12.
- Protocol: `signal` (§5.12), `thd` (§5.13). `fingerprint` reports THD+N and harmonics with probes 0.1.4.

## 0.1.0 (unreleased, 2026-11-03)

First complete version, tested in Ableton Live 12.4.16 on macOS 27 (Apple Silicon).

- **Remote Script** `LiveCLI` (local JSON API, reloadable without restarting Live) and the `live` command, `--json` everywhere, Italian aliases, extensions.
- Song and transport, tracks (create, delete, duplicate, routing in/out, arm, monitor), mixer (volume, pan, split pan, sends, activator, crossfade, cue), devices (parameters by display value also on AU/VST plugins, insert Live devices, load from the browser, replace safely with hardware inserts, A/B compare, latency per device), racks (chains, variations, Drum Rack pads), scenes and clips (properties, bulk changes, quantize, transpose, MIDI notes, automation envelopes, arrangement clips), locators, undo grouping.
- `inspect` to discover what Live's API offers on any object.
- `snapshot` / `diff` / `restore` / `events`: a safety net and a live view of changes.
- `bounce`: record a track or the master inside Live with punch in/out (the API has no export).
- `plugins`: installed plugins and Live devices by category, style and emulated hardware (`livecli/catalog.json`, plus a user catalog).
- `als`: read a Live set without opening it.
- **Live Probe Protocol v0.1** (`docs/PROTOCOL.md`): probes that publish real-time measurements over a local socket; reference probe (VST3, MIT) and a mock probe.
- Probe commands: `probes`, `probe-add`, `measure`, `watch`, `compare` (band gain across a plugin or a hardware insert), `mix`, `latency` (real vs assumed latency of a device), `sync` / `grid` (timing against the grid, Track Delay for Overbridge and hardware on MIDI clock).
- Tests: the real `commands.py` on a fake Live API, probe commands on the mock probe.
