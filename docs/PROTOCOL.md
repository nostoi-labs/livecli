# Live Probe Protocol — v0.1 (draft)

A small, open protocol that lets **audio probes** (plugins placed on tracks in a DAW) publish real-time measurements to local tools, such as the `live` CLI or an AI agent. Any plugin that implements it shows up in `live probes`. **Loudline Probe** is one implementation; the reference probe in `probes/reference/` is another.

Status: draft 0.1 (2026-11-03). Breaking changes are allowed until 1.0.

## 1. Transport and discovery

- Each **host process** that contains probes runs **one server**: a Unix domain stream socket.
  - In Ableton Live, VST3 plugins run inside the Live process, so all instances of one probe product share one server. Two products, such as Loudline Probe and the reference probe, each run their own server.
- Socket path: `~/Library/Application Support/LiveProbes/<product>-<pid>.sock`
  - `<product>` is lowercase `[a-z0-9-]`, for example `loudline-probe` or `reference-probe`.
  - The directory is created with mode `0700` if missing. The socket is `0600`.
  - On startup, a server deletes stale sockets of its own product whose `<pid>` is no longer alive.
  - When the last instance is destroyed, the server closes and deletes its socket.
- macOS limits a Unix socket path to 104 bytes. If the path above would be longer (long user names), the server uses `/tmp/LiveProbes-<uid>/<product>-<pid>.sock` instead (directory `0700`), where `<uid>` is the numeric user id.
- Clients find servers by listing `*.sock` in both directories. Every socket is tried; a socket that refuses connections is ignored.
- Local machine only: no TCP, no network.
- Optional UDP fallback, for environments that cannot open Unix sockets (Max for Live): see §7.

## 2. Framing

- UTF-8 JSON, **one object per line** (`\n`-terminated). No embedded newlines.
- Requests come from the client. Every request has `"cmd"` and may have `"rid"` (request id, echoed back).
- Responses: `{"ok": true, "rid": ..., ...}` or `{"ok": false, "rid": ..., "error": "message", "code": "not_found|bad_request|unsupported|busy"}`.
- A connection can carry many requests in sequence. After `subscribe`, the server also pushes event lines (§5.6) on that connection.
- Numbers: floats in dB with 1 decimal place are enough. Silence is reported as `-150.0`, not `-inf`.

## 3. Instances and identity

Every probe instance has:

| field | type | meaning |
|---|---|---|
| `uid` | string | unique per instance for the life of the process (e.g. `"lp-7f3a-0003"`). Never reused while the process lives |
| `probe_id` | int | user-settable **plugin parameter** "Probe ID", 0 to 999, default 0. Saved with the project. Lets a controller (the `live` CLI via Live's API) tag an instance and find it again |
| `track` | string or null | track name from the host (VST3 `Vst::ChannelContext::IInfoListener`, key `kChannelNameKey`). Live provides it |
| `track_index` | int or null | `kChannelIndexKey`, when provided |
| `track_color` | string or null | `#RRGGBB` from `kChannelColorKey` |
| `label` | string | free text set by a client with `set_label` (not saved), default `""` |
| `channels` | int | 1 or 2 |
| `sample_rate` | float | current host sample rate |
| `latency_samples` | int | latency the probe itself reports to the host. Must be 0: probes pass audio through untouched |
| `active` | bool | audio processed in the last 1 s |
| `signal` | bool | RMS above -90 dBFS in the last 1 s |

**The probe never alters audio.** Output equals input, bit for bit, at zero latency.

## 4. Measurements

All analysis runs **off the audio thread**. The audio thread only copies samples into a lock-free ring buffer, together with the host sample position and a monotonic timestamp of the block.

### 4.1 Snapshot (`get`)
```json
{
  "uid": "lp-7f3a-0003", "t": 1234.567, "window_s": 0.3,
  "peak_db": [-6.1, -6.4], "rms_db": [-18.2, -18.5],
  "true_peak_dbtp": -5.8,
  "lufs_m": -14.1, "lufs_s": -14.6, "lufs_i": -15.0, "lra": 4.2,
  "plr": 9.2, "crest_db": 12.1,
  "correlation": 0.82, "width": 0.31,
  "mid_db": -18.0, "side_db": -29.5,
  "bands": {"centers": [20.0, 25.2, "..."], "mid": [-60.1, "..."], "side": [-75.0, "..."]},
  "bass_mono": 0.97,
  "since_reset_s": 42.0
}
```
- `t`: seconds on the server's monotonic clock (the same clock for all instances of one server).
- Required fields for every probe: `peak_db`, `rms_db`. Everything else is **optional**. A probe lists what it supports in `hello.capabilities`.
- `peak_db`: sample peak per channel over `window_s`. `true_peak_dbtp`: max true peak (4× oversampled, ITU-R BS.1770) since reset.
- `lufs_m` (400 ms), `lufs_s` (3 s), `lufs_i` (integrated since reset, gated), `lra` (EBU Tech 3342): BS.1770 / EBU R128.
- `plr`: true peak minus `lufs_i`. `crest_db`: peak minus RMS over the window.
- `correlation`: -1…1, stereo phase correlation over the window. `width`: side energy / (mid + side) energy, 0…1.
- `bands`: **31 one-third-octave bands, centers `20 · 2^(k/3)` Hz, k = 0…30** (20 Hz … 20.5 kHz). Energy in dBFS per band (sum of FFT bin power inside the band edges `center · 2^(±1/6)`), mid (`(L+R)/2`) and side (`(L−R)/2`). Averaging: exponential, time constant `window_s`. Mono probes: `side` is omitted.
- `bass_mono`: correlation of the low end (< 120 Hz), -1…1.
- A request can set `"window_s"` (0.1…10). The default is 0.3.

### 4.2 History
The server keeps, per instance, a ring of **1-second summaries for the last 120 s** (at least 60):
```json
{"t": 1230.0, "peak_db": [..], "rms_db": [..], "lufs_m": .., "lufs_s": .., "true_peak_dbtp": .., "correlation": .., "bands_mid": [31 values], "bands_side": [31 values]}
```
`t` is the start of the second, aligned to whole seconds of the server clock, so summaries of different instances line up.

### 4.3 Compare (optional, capability `compare`)
Because all instances of one server live in the same process and timestamp their blocks with the same clock, the server can compare two instances **on the same samples**:
- `gain_db[31]`: band energy B minus A, over the same window.
- With capability `transfer`: transfer function estimate H(f) = P_ab / P_aa (Welch, Hann, 50% overlap) on 1/6-octave points, with `coherence[f]` (0…1) and `delay_samples` (cross-correlation peak, ±1 s search). This is the real frequency response of whatever sits between A and B (a plugin, a rack, an external hardware insert), measured on program material.

If the server does not support `compare`, the client computes `gain_db` itself from two `history` responses (same `t` values).

## 5. Commands

### 5.1 `hello`
→ `{"cmd":"hello"}`
← `{"ok":true,"protocol":"0.1","product":"Loudline Probe","version":"0.1.0","pid":4242,"host":"Ableton Live 12.4.7","capabilities":["peak","rms","true_peak","lufs","lra","bands","stereo","history","compare","transfer","delay","reset","subscribe","label"]}`

### 5.2 `list`
→ `{"cmd":"list"}`
← `{"ok":true,"instances":[{identity fields of §3}, ...]}`

### 5.3 `get`
→ `{"cmd":"get","uid":"lp-7f3a-0003","window_s":1.0}` (`uid` or `probe_id`. With `"all":true` it returns `"snapshots":[...]` for every instance)
← `{"ok":true,"snapshot":{...§4.1}}`

### 5.4 `history`
→ `{"cmd":"history","uid":"...","seconds":30}`
← `{"ok":true,"uid":"...","summaries":[...§4.2]}` (oldest first)

### 5.5 `compare`
→ `{"cmd":"compare","a":"<uid>","b":"<uid>","seconds":10,"transfer":true}`
← `{"ok":true,"seconds":10,"centers":[31],"gain_db":[31],"transfer":{"freqs":[..],"mag_db":[..],"coherence":[..],"delay_samples":128}}`

### 5.6 `subscribe` / `unsubscribe`
→ `{"cmd":"subscribe","uid":"...","hz":10,"fields":["peak_db","rms_db","lufs_m"]}` (`hz` 1…30)
← `{"ok":true}`, then event lines `{"event":"snapshot","uid":"...","snapshot":{...}}` until `unsubscribe` or disconnect.
Also pushed to every subscriber: `{"event":"instance_added|instance_removed|instance_changed","instance":{...}}`.

### 5.7 `reset`
→ `{"cmd":"reset","uid":"..."}` (or `"all":true`): clears integrated LUFS, LRA, max true peak and history.

### 5.8 `set_label`
→ `{"cmd":"set_label","uid":"...","label":"pre soothe2"}`

### 5.9 `delay` (optional, capability `delay`)
→ `{"cmd":"delay","a":"<uid>","b":"<uid>","seconds":5,"max_lag_ms":1000}` (`a`/`b` may also be probe_id numbers; same server)
← `{"ok":true,"a":"...","b":"...","delay_samples":480,"delay_ms":10.0,"correlation_peak":0.99,"polarity":1,"ambiguity":0.05,"sample_rate":48000.0,"seconds":5.0,"aligned":"block"}`
- `delay_samples`: integer, **positive = B arrives after A**. Found with normalized cross-correlation over `seconds` of audio, searching ±`max_lag_ms` (1…1000, default 1000); `seconds` 0.5…10, default 5.
- `correlation_peak`: 0…1, |normalized correlation| at the peak. Above ~0.5 the result is reliable.
- `polarity`: 1, or -1 when the peak is negative (polarity inverted between A and B).
- `ambiguity`: 0…1, second peak / first peak (outside ±1 ms of the first). Near 1 = periodic signal (a loop, a steady tone): the delay is not unique. Use noise or music; aim for < 0.5.
- `aligned`: `"block"` = A and B sampled in the same audio cycle (exact to the sample); `"clock"` = aligned by timestamps (a few samples of possible error, e.g. probes on different tracks).
- Timing: capture starts at the first request for that pair and stays on for 120 s after the last one. The first answer takes about `seconds + 2·max_lag + 0.5` s, later ones are immediate. Clients should wait at least `seconds + 2·max_lag + 3` s. Other requests on the same connection keep being answered meanwhile, so answers can arrive out of order: match them by `rid`.
- Errors: `not_found`; `bad_request` (a == b, different sample rates, values out of range); `busy` (A or B without signal, below -90 dBFS, or not enough audio yet); `unsupported`.

## 6. Plugin requirements (VST3)
- Stereo in/out (mono variant optional). Pass-through, zero latency, no tail.
- Parameters: `Probe ID` (0…999, stepCount 999, shown as an integer) and nothing else that affects processing. It must be **automatable** (`kCanAutomate`): Ableton Live hides VST3 parameters that are not automatable, and the CLI sets the ID through Live's API. Few parameters, so Live exposes them without configuration.
- Implement `IInfoListener` for the track name, index and color. Re-send `instance_changed` when they change.
- No UI is required. A minimal view (ID, track, product link) is allowed.
- CPU: the audio-thread cost must be a memcpy into the ring buffer. Analysis must be shared and throttled (≤ 30 Hz per instance) and must stop for instances without signal.

## 7. UDP fallback (optional, for Max for Live and similar)
Push only, no commands: a probe sends `{"event":"snapshot","product":"m4l-probe","uid":"...","probe_id":N,"track":"...","snapshot":{...}}` datagrams to `127.0.0.1:9879` at ≤ 10 Hz. A client that wants these binds that port. Max datagram size: 8 KB.

## 8. Versioning
`protocol` is `MAJOR.MINOR`. Clients accept servers with the same MAJOR, ignore unknown fields, and check `capabilities` before using optional commands.
