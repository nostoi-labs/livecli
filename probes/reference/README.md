# Reference Probe (VST3, MIT)

A minimal implementation of the [Live Probe Protocol](../../docs/PROTOCOL.md) v0.1: a pass-through VST3 with one "Probe ID" parameter that publishes peak, RMS, stereo correlation/width, mid/side level and 31 one-third-octave bands of mid and side on a Unix socket. The `live` CLI finds it with `live probes`.

It is also the shared container for other probe products: `core/` (protocol, socket server, instances, lock-free ring, analysis thread) and `vst3/` (the plug-in) know nothing about the DSP. A product supplies an `Engine` and a `ProductInfo` (`core/engine.h`); `engine/` is the reference one.

## Layout

| path | what |
|---|---|
| `core/ring.h` | SPSC lock-free ring: the audio thread copies whole blocks (samples + timestamp), drops blocks when full, never blocks |
| `core/instance.*` | one probe instance: identity, 10 ms ticks for peak/RMS/correlation windows, band frames with exponential averaging (time constant `window_s`), 1-second history (128 s) aligned on whole seconds of the shared clock |
| `core/hub.*` | per-process registry; the first instance starts the shared analysis thread (30 Hz) and the server, the last one stops them |
| `core/server.*` | protocol §2-5 and the Unix socket server (§1: directory `0700`, socket `0600`, stale sockets removed, `/tmp/LiveProbes-<uid>` when the path exceeds 104 bytes, `LIVEPROBES_DIR` override) |
| `core/json.*` | small JSON reader/writer (UTF-8 safe) |
| `core/delay.*` | `delay` command: FFT cross-correlation of two instances' captured mid signals |
| `engine/` | reference DSP: Hann-windowed FFT (16384 points at 44.1/48 kHz), band power summed inside `center · 2^(±1/6)`, normalised so a sine of peak A reads A²/2 (-9.0 dB for a -6 dBFS sine) |
| `vst3/` | the plug-in: stereo or mono, 32-bit float only, zero latency, no tail, `IInfoListener` for track name/index/color |
| `tests/` | unit tests, a real-time VST3 test host and a socket test client |

Real-time rules on the audio thread: `process()` copies input to output (skipped when the host processes in place) and `memcpy`s the block into the ring with a monotonic timestamp. No locks, no allocation, no system calls. Analysis stops running the FFT after 3.5 s of digital silence.

## Build (macOS, Apple Silicon)

Needs CMake ≥ 3.25, Ninja, Xcode and the [VST3 SDK](https://github.com/steinbergmedia/vst3sdk) 3.8 or later (MIT):

```sh
git clone --recursive --branch v3.8.1_build_84 https://github.com/steinbergmedia/vst3sdk
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DVST3_SDK_ROOT=$PWD/vst3sdk
cmake --build build          # also runs the SDK validator on the plug-in
./build/probe_tests          # unit tests (no SDK needed: configure without VST3_SDK_ROOT)
```

The bundle is `build/VST3/Release/ReferenceProbe.vst3`. Copy it to `~/Library/Audio/Plug-Ins/VST3` or `/Library/Audio/Plug-Ins/VST3`, then rescan plug-ins in Live.

`scripts/package-macos.sh` signs it with a Developer ID, builds a `.pkg` for `/Library/Audio/Plug-Ins/VST3`, notarizes and staples it (identities and notary credentials come from environment variables).

## Test without a DAW

```sh
# 7 instances in real time; the host checks on every block that the output equals the input bit for bit
./build/bin/Release/probe_vst3host build/VST3/Release/ReferenceProbe.vst3 40 \
    sine:1000:-6 sine:1000:-12@inplace anti:-20 silence pre=noise:-20 post=eqnoise:-20:1000:6 Mono=sine:440:-10@mono &
sleep 14
live compare pre post 10                                                   # +6 dB peaking EQ at 1 kHz
python3 tests/probe_client_test.py --product reference-probe --testhost   # every command, with numbers
```

## Notes

- `Probe ID` is a `RangeParameter` 0…999 with 999 steps, shown as an integer. It is marked automatable (CMake option `PROBE_ID_AUTOMATABLE`, default ON) because Live does not list VST3 parameters that are not automatable, and the CLI sets the ID through Live's parameter list.
- `delay` (capability `delay`, not yet in PROTOCOL.md): `{"cmd":"delay","a":uid,"b":uid,"seconds":5,"max_lag_ms":1000}` → `{"ok":true,"delay_samples":480,"delay_ms":10.0,"correlation_peak":0.99,"polarity":1,"ambiguity":0.05,"sample_rate":48000.0,"seconds":5.0,"aligned":"block"}`. Positive = B late. Cross-correlation of the mid signals (FFT, server thread), aligned on the same audio cycle (`"block"`) or, failing that, on the clock (`"clock"`). The capture of an instance starts with its first `delay` request and stops 120 s after the last one, so the first answer comes after about `seconds + 2·max_lag + 0.5` s, possibly after other answers on the same connection (match `rid`). `ambiguity` near 1 means a periodic signal. Errors: `busy` (no signal, not enough audio), `bad_request` (same instance, different sample rates).
- `compare` is implemented in the server but not advertised in `hello.capabilities` yet: clients compute it from `history`.
- Product id `reference-probe`, plug-in name "Reference Probe", bundle id `org.livecli.reference-probe`, VST3 class ids in `vst3/entry_reference.cpp`. Another product must use its own ids.
