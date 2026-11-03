// SPDX-License-Identifier: MIT
#include "reference_engine.h"

#include <algorithm>
#include <cmath>

#ifdef __APPLE__
#include <Accelerate/Accelerate.h>
#endif

namespace liveprobe {

ReferenceEngine::ReferenceEngine() = default;

ReferenceEngine::~ReferenceEngine() {
#ifdef __APPLE__
    if (setup_) vDSP_destroy_fftsetup((FFTSetup)setup_);
#endif
}

void ReferenceEngine::prepare(double sr) {
    // ~2.9 Hz bins at any rate: the 20 Hz band (17.8-22.4 Hz) still holds a bin or two
    n_ = sr <= 50000 ? 16384 : sr <= 100000 ? 32768 : 65536;
    log2n_ = (int)std::lround(std::log2((double)n_));
    hop_ = n_ / 8;  // ~23 frames/s at 48 kHz
    pos_ = sinceHop_ = filled_ = 0;
    histM_.assign(n_, 0.f);
    histS_.assign(n_, 0.f);
    re_.assign(n_, 0.f);
    im_.assign(n_, 0.f);
    window_.resize(n_);
    double sumw2 = 0;
    for (int i = 0; i < n_; i++) {
        double w = 0.5 - 0.5 * std::cos(2 * M_PI * i / n_);  // periodic Hann
        window_[i] = (float)w;
        sumw2 += w * w;
    }
    // one-sided bin power -> mean square: a sine of peak A sums to A²/2 over its bins
    norm_ = 2.0 / ((double)n_ * sumw2);
    bandOf_.assign(n_ / 2, -1);
    for (int k = 1; k < n_ / 2; k++) {
        double f = k * sr / n_;
        int b = (int)std::floor(3.0 * std::log2(f / 20.0) + 0.5);  // edges at center · 2^(±1/6)
        if (b >= 0 && b < kBands) bandOf_[k] = b;
    }
#ifdef __APPLE__
    if (setup_) vDSP_destroy_fftsetup((FFTSetup)setup_);
    setup_ = vDSP_create_fftsetup(log2n_, kFFTRadix2);
#else
    cosT_.resize(n_ / 2);
    sinT_.resize(n_ / 2);
    for (int i = 0; i < n_ / 2; i++) {
        cosT_[i] = (float)std::cos(2 * M_PI * i / n_);
        sinT_[i] = (float)-std::sin(2 * M_PI * i / n_);
    }
    rev_.resize(n_);
    for (int i = 0; i < n_; i++) {
        int r = 0;
        for (int b = 0; b < log2n_; b++) if (i & (1 << b)) r |= 1 << (log2n_ - 1 - b);
        rev_[i] = r;
    }
#endif
}

void ReferenceEngine::process(const float* l, const float* r, int n) {
    for (int i = 0; i < n; i++) {
        histM_[pos_] = 0.5f * (l[i] + r[i]);
        histS_[pos_] = 0.5f * (l[i] - r[i]);
        pos_ = (pos_ + 1) & (n_ - 1);
        if (filled_ < n_) filled_++;
        if (++sinceHop_ >= hop_) {
            sinceHop_ = 0;
            frame();
        }
    }
}

void ReferenceEngine::fft() {
#ifdef __APPLE__
    DSPSplitComplex z{re_.data(), im_.data()};
    vDSP_fft_zip((FFTSetup)setup_, &z, 1, log2n_, kFFTDirection_Forward);
#else
    for (int i = 0; i < n_; i++) {
        int j = rev_[i];
        if (j > i) { std::swap(re_[i], re_[j]); std::swap(im_[i], im_[j]); }
    }
    for (int len = 2; len <= n_; len <<= 1) {
        int half = len >> 1, step = n_ / len;
        for (int i = 0; i < n_; i += len) {
            for (int k = 0; k < half; k++) {
                float wr = cosT_[k * step], wi = sinT_[k * step];
                float xr = re_[i + k + half], xi = im_[i + k + half];
                float tr = xr * wr - xi * wi, ti = xr * wi + xi * wr;
                re_[i + k + half] = re_[i + k] - tr;
                im_[i + k + half] = im_[i + k] - ti;
                re_[i + k] += tr;
                im_[i + k] += ti;
            }
        }
    }
#endif
}

void ReferenceEngine::frame() {
    // oldest sample first: the ring from pos_ to the end, then from the start
    for (int i = 0; i < n_; i++) {
        int p = (pos_ + i) & (n_ - 1);
        re_[i] = histM_[p] * window_[i];
        im_[i] = histS_[p] * window_[i];
    }
    fft();
    // z = m + j·s, both real: M_k = (Z_k + conj Z_{N-k}) / 2, S_k = (Z_k - conj Z_{N-k}) / 2j
    BandFrame f;
    double mid[kBands] = {}, side[kBands] = {};
    for (int k = 1; k < n_ / 2; k++) {
        int b = bandOf_[k];
        if (b < 0) continue;
        double ar = re_[k], ai = im_[k], br = re_[n_ - k], bi = im_[n_ - k];
        double mr = 0.5 * (ar + br), mi = 0.5 * (ai - bi);
        double sr = 0.5 * (ai + bi), si = -0.5 * (ar - br);
        mid[b] += mr * mr + mi * mi;
        side[b] += sr * sr + si * si;
    }
    for (int b = 0; b < kBands; b++) {
        f.mid[b] = (float)(mid[b] * norm_);
        f.side[b] = (float)(side[b] * norm_);
    }
    if (onBandFrame) onBandFrame(f);
}

}  // namespace liveprobe
