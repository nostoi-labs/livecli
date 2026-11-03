// SPDX-License-Identifier: MIT
// Live Probe core: transient (onset) detection with the host's song position (command "onsets").
// Runs on the analysis thread, only while a client has asked for onsets.
#pragma once

#include <cstdint>
#include <vector>

#include "ring.h"

namespace liveprobe {

struct Onset {
    double t = 0;        // server clock of the onset sample
    double ppq = 0;      // song position in quarter notes
    double tempo = 0;    // bpm of the block
    double levelDb = 0;  // peak of the transient (high-passed), dBFS
};

class OnsetDetector {
public:
    /// Resets everything; the first 50 ms after it cannot trigger.
    void prepare(double sampleRate);
    /// Lowest level that can trigger (the strictest client filters further).
    void setThresholdDb(double db);
    /// One block, as it left the audio thread. Finished onsets go to `out` (playing, music valid)
    /// or are counted in `stoppedOnsets` (transport stopped or no song position).
    void process(const float* l, const float* r, int n, const BlockInfo& bi, std::vector<Onset>& out);

    long stoppedOnsets = 0;

private:
    struct Biquad {
        double b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0, x1 = 0, x2 = 0, y1 = 0, y2 = 0;
        double run(double x) {
            double y = b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2;
            x2 = x1; x1 = x; y2 = y1; y1 = y;
            return y;
        }
    };
    struct Block {
        uint64_t idx = 0;
        BlockInfo info;
    };
    void finish(std::vector<Onset>& out);
    bool locate(double pos, Onset& o, bool& playing) const;

    double sr_ = 0, thrLin_ = 0.00316;  // -50 dBFS
    Biquad hpL_, hpR_;
    std::vector<float> hist_;            // |high-passed| (max of the channels), by stream index
    uint64_t mask_ = 0, idx_ = 0;
    std::vector<Block> blocks_;          // recent blocks, ring
    size_t blockHead_ = 0, blockCount_ = 0;
    double env_ = 0, floor_ = 0, relFast_ = 0, floorDown_ = 0, floorUp_ = 0;
    bool collecting_ = false;
    uint64_t trigger_ = 0, lastOnset_ = 0;
    bool haveLast_ = false;
    long win_ = 0, look_ = 0, refractory_ = 0;
};

}  // namespace liveprobe
