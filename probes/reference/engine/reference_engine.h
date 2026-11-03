// SPDX-License-Identifier: MIT
// Reference Probe DSP: 31 one-third-octave bands of mid and side from a Hann-windowed FFT.
// Peak, RMS, correlation and width come from the shared core.
#pragma once

#include <vector>

#include "engine.h"

namespace liveprobe {

class ReferenceEngine : public Engine {
public:
    ReferenceEngine();
    ~ReferenceEngine() override;
    void prepare(double sampleRate) override;
    void process(const float* l, const float* r, int n) override;
    void reset() override {}
    bool hasBands() const override { return true; }

    int fftSize() const { return n_; }
    int hop() const { return hop_; }

private:
    void frame();
    void fft();

    int n_ = 0, log2n_ = 0, hop_ = 0;
    int pos_ = 0, sinceHop_ = 0, filled_ = 0;
    double norm_ = 0;
    std::vector<float> histM_, histS_, window_, re_, im_;
    std::vector<int> bandOf_;  // bin -> band index, -1 outside 20 Hz…22.6 kHz
    void* setup_ = nullptr;   // vDSP FFT setup on Apple
    std::vector<float> cosT_, sinT_;  // portable FFT twiddles
    std::vector<int> rev_;
};

}  // namespace liveprobe
