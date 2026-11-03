// SPDX-License-Identifier: MIT
// Live Probe core: what a product plugs into the shared container.
#pragma once

#include <cmath>
#include <functional>
#include <memory>
#include <string>
#include <vector>

namespace liveprobe {

constexpr int kBands = 31;
/// Protocol §4.1: centers 20 · 2^(k/3) Hz, k = 0…30.
inline double bandCenter(int k) { return 20.0 * std::pow(2.0, k / 3.0); }

/// One analysis frame of band energies, linear power on the mean-square scale: the sum over all
/// bands equals the mean square of the signal (a sine of peak A reads A²/2 in its band).
struct BandFrame {
    float mid[kBands];
    float side[kBands];
};

/// Loudness readings in LUFS / LU / dBTP. NaN = not supported or not available.
struct LoudnessValues {
    double lufsM = NAN, lufsS = NAN, lufsI = NAN, lra = NAN, truePeakMax = NAN;
};

/// Per-instance analysis engine. Every call happens on the analysis thread or, for the readout
/// calls, on the server thread, always under the instance's analysis lock: no locking needed inside.
class Engine {
public:
    virtual ~Engine() = default;
    /// (Re)build for a sample rate. Clears everything.
    virtual void prepare(double sampleRate) = 0;
    /// Analysis thread: `n` <= 4096 frames. Completed band frames go to `onBandFrame`.
    virtual void process(const float* l, const float* r, int n) = 0;
    /// Clears integrated loudness, LRA and max true peak.
    virtual void reset() = 0;
    virtual bool hasBands() const = 0;
    /// Analysis thread, before process(): 1 = mono instance (then r == a copy of l), 2 = stereo.
    virtual void setChannels(int channels) {}
    /// `integrated`: also compute lufs_i and lra (may be slower; the engine may cache them).
    virtual void loudness(LoudnessValues&, bool integrated) {}
    /// Max true peak (dBTP) since the previous call, for the 1-second history. NaN if unsupported.
    virtual double takeTruePeak() { return NAN; }

    std::function<void(const BandFrame&)> onBandFrame;
};

struct ProductInfo {
    std::string id;         // protocol product id, e.g. "reference-probe"
    std::string name;       // e.g. "Reference Probe"
    std::string version;    // e.g. "0.1.0"
    std::string uidPrefix;  // e.g. "rp"
    std::vector<std::string> capabilities;
    std::function<std::unique_ptr<Engine>()> makeEngine;
};

/// Defined once by each product (reference-probe, loudline-probe, tests).
const ProductInfo& product();

}  // namespace liveprobe
