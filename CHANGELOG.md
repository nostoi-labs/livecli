# Changelog

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
