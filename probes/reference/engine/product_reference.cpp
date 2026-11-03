// SPDX-License-Identifier: MIT
#include "engine.h"
#include "reference_engine.h"

#ifndef PROBE_VERSION
#define PROBE_VERSION "0.1.0"
#endif

namespace liveprobe {

const ProductInfo& product() {
    // leaked on purpose: never destroyed by exit() while the server thread may still read it
    static const ProductInfo& info = *new ProductInfo{
        "reference-probe",
        "Reference Probe",
        PROBE_VERSION,
        "rp",
        {"peak", "rms", "bands", "stereo", "history", "reset", "subscribe", "label", "delay", "onsets", "pulse"},
        [] { return std::unique_ptr<Engine>(new ReferenceEngine()); },
    };
    return info;
}

}  // namespace liveprobe
