# What Live's API cannot do (and what to do instead)

Verified with Ableton Live 12.4.16 on macOS 27 using `live inspect`. Run `live inspect song|app|track <t>|mixer <t>|device <t> <d>` on your own Live version: the API changes between releases.

| Operation | Status in the API | What to do |
|---|---|---|
| Save, open or export the set; render | not exposed (no method on Song or Application) | ask the user; for audio use `live bounce` (real-time recording inside Live) |
| Track Delay | not exposed | `live sync` / `live grid` measure the value; the user types it in the mixer |
| Delay Compensation, Reduced Latency When Monitoring (Options menu) | not exposed | ask the user; `live latency` tells you whether a device is compensated correctly |
| Hardware Latency of External Audio Effect | not a parameter (the device exposes Device On, Dry/Wet, Output and Input Gain only) | `live latency` measures the real delay and tells how much to add or remove; the user sets it |
| Audio preferences (device, buffer, sample rate, driver error compensation) | not exposed | never change them without the user |
| Plugin parameters not configured in Live | invisible (a plugin publishes only what it exposes; some show only Device On) | ask the user to use Configure in the device title bar; then `live set` works |
| Plugin internal presets | some plugins only: see `live preset` | the plugin's own browser |
| Arrangement automation of track/device parameters | read/write only as clip envelopes on session clips; arrangement clip envelopes are refused | session clip envelopes (`live envelope-set`), or the user draws them |
| Creating group tracks, freeze, flatten | not exposed | ask the user |
| Max for Live modulator mapping (Map button) | not exposed | ask the user |
| Native devices by name in `insert` | only Live's built-in devices; Max for Live devices (Align Delay, LFO…) must be loaded from the browser | `live load <track> audio_effects/<name>` |
| Pad names in a Drum Rack | read-only | rename the chain inside the pad |

## Traps found while testing
- **Automatic arm.** Live arms the selected MIDI track on its own (`implicit_arm`). Any arrangement recording also records on it, overwriting its clips in the recorded region. `live bounce` selects its own track and refuses to record while any other track is armed in any way.
- **Back to Arrangement.** After a session clip plays, a track ignores the arrangement until "Back to Arrangement". `live back-to-arrangement` resets it per track (setting it only on the song is not enough).
- **Play position.** With the transport stopped, Live starts from its own start marker and ignores the playhead set by the API: move the playhead after starting playback.
- **Recorded clip offsets.** A recorded clip's beat 0 is not the file's first sample: use the clip's warp markers to map beats to seconds.
- **Display rounding.** Setting by display text needs a final check of the neighbouring raw values ("-3.0 dB" vs "-3.1 dB"), and units change inside one parameter ("600 ms" then "1.00 s").
- **Pan.** Live's pan text is not monotonic (50L … C … 50R): convert L/C/R to raw values.
- **Automation boundaries.** `value_at_time` at the exact start of a step returns the previous value.
- **Touching a parameter to convert text** marks its automation as overridden: convert text to values without writing to the parameter.
- **Cue points** can be created only with the transport stopped, one tick after moving the playhead.
