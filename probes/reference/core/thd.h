// SPDX-License-Identifier: MIT
// Live Probe core: harmonic distortion of a sine in the probe's own samples (command "thd", §5.13).
// Runs on the server thread over a recording made by the analysis thread, only on request.
#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace liveprobe {

struct ThdResult {
    bool ok = false;
    std::string code, error;
    double freq = 0;            // measured fundamental, Hz (sub-bin)
    double fundamentalDb = 0;   // peak amplitude of the fundamental, dBFS (a -20 dBFS sine reads -20)
    double thdPct = 0, thdDb = 0;      // harmonics 2…10 inside the band, against the fundamental
    double thdNPct = 0, thdNDb = 0;    // everything in 20 Hz…20 kHz except the fundamental (±1/2 octave notch)
    double noiseDb = 0;         // RMS of what is left without fundamental notch and harmonics, dBFS
    double snrDb = 0;           // fundamental RMS / noise RMS
    std::vector<double> harmonicsDb;  // H2…H10 in dBc; NaN beyond the band
    bool clipping = false;      // samples at or above -0.1 dBFS
    int frames = 0;             // FFT frames averaged
    int fftSize = 0;
};

/// `freqHint` <= 0: find the strongest component between 20 Hz and 0.45 · sampleRate.
ThdResult measureThd(const float* x, size_t n, double sampleRate, double freqHint);

/// FFT size used at a sample rate (≈ 0.7 Hz bins).
int thdFftSize(double sampleRate);

}  // namespace liveprobe
