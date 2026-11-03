// SPDX-License-Identifier: MIT
// Live Probe core: delay between two instances (command "delay", capability "delay").
// Cross-correlation of the mid signals captured on the analysis side, aligned on the same audio
// cycle, FFT-based. Runs on the server thread, only when a client asks.
#pragma once

#include <string>

#include "instance.h"

namespace liveprobe {

struct DelayResult {
    bool ok = false;
    bool retry = false;        // not enough audio yet: ask again later
    std::string code, error;   // when !ok
    long long delaySamples = 0;  // positive: B late versus A
    double correlationPeak = 0;  // |normalised correlation| at the peak, 0…1
    int polarity = 1;
    double ambiguity = 0;      // second peak / first peak (outside ±1 ms)
    double sampleRate = 0;
    double seconds = 0;
    const char* aligned = "block";
};

DelayResult measureDelay(const Instance::CaptureCopy& a, const Instance::CaptureCopy& b, double seconds, double maxLagS);

}  // namespace liveprobe
