// SPDX-License-Identifier: MIT
#include "delay.h"

#include <algorithm>
#include <cmath>
#include <complex>
#include <vector>

#ifdef __APPLE__
#include <Accelerate/Accelerate.h>
#endif

namespace liveprobe {

namespace {

/// c[k] = Σ_n x[n] · y[n + k] for k = 0 … y.size() - x.size(), up to a constant positive scale.
std::vector<double> crossCorrelate(const std::vector<float>& x, const std::vector<float>& y) {
    size_t n = 1;
    int log2n = 0;
    while (n < y.size()) { n <<= 1; log2n++; }  // y.size() ≥ x.size() + lags: no circular wrap
    const size_t lags = y.size() - x.size() + 1;
    std::vector<double> c(lags);
#ifdef __APPLE__
    std::vector<float> xr(n / 2), xi(n / 2), yr(n / 2), yi(n / 2), buf(n, 0.f);
    FFTSetup setup = vDSP_create_fftsetup(log2n, kFFTRadix2);
    DSPSplitComplex X{xr.data(), xi.data()}, Y{yr.data(), yi.data()};
    std::copy(x.begin(), x.end(), buf.begin());
    vDSP_ctoz((const DSPComplex*)buf.data(), 2, &X, 1, n / 2);
    std::fill(buf.begin(), buf.end(), 0.f);
    std::copy(y.begin(), y.end(), buf.begin());
    vDSP_ctoz((const DSPComplex*)buf.data(), 2, &Y, 1, n / 2);
    vDSP_fft_zrip(setup, &X, 1, log2n, kFFTDirection_Forward);
    vDSP_fft_zrip(setup, &Y, 1, log2n, kFFTDirection_Forward);
    // conj(X) · Y; DC and Nyquist are packed as two real values in bin 0
    const float dc = xr[0] * yr[0], ny = xi[0] * yi[0];
    for (size_t k = 1; k < n / 2; k++) {
        float a = xr[k], b = xi[k], cr = yr[k], ci = yi[k];
        yr[k] = a * cr + b * ci;
        yi[k] = a * ci - b * cr;
    }
    yr[0] = dc;
    yi[0] = ny;
    vDSP_fft_zrip(setup, &Y, 1, log2n, kFFTDirection_Inverse);
    vDSP_ztoc(&Y, 1, (DSPComplex*)buf.data(), 2, n / 2);
    vDSP_destroy_fftsetup(setup);
    for (size_t k = 0; k < lags; k++) c[k] = buf[k];
#else
    using cd = std::complex<double>;
    std::vector<cd> X(n), Y(n);
    for (size_t i = 0; i < x.size(); i++) X[i] = x[i];
    for (size_t i = 0; i < y.size(); i++) Y[i] = y[i];
    auto fft = [&](std::vector<cd>& a, bool inv) {
        for (size_t i = 1, j = 0; i < n; i++) {
            size_t bit = n >> 1;
            for (; j & bit; bit >>= 1) j ^= bit;
            j ^= bit;
            if (i < j) std::swap(a[i], a[j]);
        }
        for (size_t len = 2; len <= n; len <<= 1) {
            double ang = 2 * M_PI / len * (inv ? 1 : -1);
            cd wl(std::cos(ang), std::sin(ang));
            for (size_t i = 0; i < n; i += len) {
                cd w(1);
                for (size_t j = 0; j < len / 2; j++) {
                    cd u = a[i + j], v = a[i + j + len / 2] * w;
                    a[i + j] = u + v;
                    a[i + j + len / 2] = u - v;
                    w *= wl;
                }
            }
        }
    };
    fft(X, false);
    fft(Y, false);
    for (size_t k = 0; k < n; k++) Y[k] = std::conj(X[k]) * Y[k];
    fft(Y, true);
    for (size_t k = 0; k < lags; k++) c[k] = Y[k].real();
#endif
    (void)log2n;
    return c;
}

/// Index in `b.blocks` of the block of the same audio cycle as A's block `ab`, or -1.
/// Pre and post on one track run one after the other in the same cycle: B starts at A's time
/// plus the processing time of what sits between them (less than one cycle).
long matchBlock(const Instance::CaptureBlock& ab, const Instance::CaptureCopy& b, double sr) {
    const double P = ab.frames / sr;
    for (long i = (long)b.blocks.size() - 1; i >= 0; i--) {
        const auto& bb = b.blocks[(size_t)i];
        if (bb.t < ab.t - 0.25 * P) break;
        if (bb.t < ab.t + 0.75 * P && bb.frames == ab.frames) return i;
    }
    return -1;
}

double energy(const float* p, size_t n) {
    double e = 0;
    for (size_t i = 0; i < n; i++) e += (double)p[i] * p[i];
    return e;
}

}  // namespace

DelayResult measureDelay(const Instance::CaptureCopy& a, const Instance::CaptureCopy& b, double seconds, double maxLagS) {
    DelayResult r;
    r.sampleRate = a.sampleRate;
    auto fail = [&](const char* code, std::string msg, bool retry = false) {
        r.ok = false;
        r.code = code;
        r.error = std::move(msg);
        r.retry = retry;
        return r;
    };
    if (a.sampleRate <= 0 || b.sampleRate <= 0 || a.blocks.empty() || b.blocks.empty())
        return fail("busy", "no audio captured yet", true);
    if (a.sampleRate != b.sampleRate) return fail("bad_request", "a and b run at different sample rates");
    const double sr = a.sampleRate;
    const long long L = (long long)std::llround(seconds * sr), M = (long long)std::llround(maxLagS * sr);

    // stream offset D (B index = A index + D) from the latest A block with a B block of the same cycle
    long long D = 0;
    bool found = false;
    for (long i = (long)a.blocks.size() - 1; i >= 0 && i >= (long)a.blocks.size() - 64; i--) {
        long j = matchBlock(a.blocks[(size_t)i], b, sr);
        if (j >= 0) {
            D = (long long)b.blocks[(size_t)j].idx - (long long)a.blocks[(size_t)i].idx;
            found = true;
            break;
        }
    }
    if (!found) {  // different block sizes or far-apart tracks: align on the clock
        const auto& ab = a.blocks.back();
        const Instance::CaptureBlock* best = &b.blocks.back();
        for (const auto& bb : b.blocks)
            if (std::fabs(bb.t - ab.t) < std::fabs(best->t - ab.t)) best = &bb;
        D = (long long)best->idx + std::llround((ab.t - best->t) * sr) - (long long)ab.idx;
        r.aligned = "clock";
    }

    // A segment [ia, ia + L) in stream indices, B window [ia + D - M, ia + D + L + M)
    const long long aFirst = (long long)a.first, aEnd = aFirst + (long long)a.x.size();
    const long long bFirst = (long long)b.first, bEnd = bFirst + (long long)b.x.size();
    long long iaEnd = std::min(aEnd, bEnd - D - M);
    long long ia = iaEnd - L;
    if (ia < aFirst || ia + D - M < bFirst) {
        const double have = (double)(iaEnd - std::max(aFirst, bFirst - D + M)) / sr;
        return fail("busy", "not enough audio captured yet (" + std::to_string((int)std::max(0.0, have)) + " s)", true);
    }
    // the offset must hold over the whole segment: check it on the block where the segment starts
    if (std::string(r.aligned) == "block") {
        for (const auto& ab : a.blocks) {
            if ((long long)ab.idx + ab.frames <= ia) continue;
            long j = matchBlock(ab, b, sr);
            if (j >= 0 && (long long)b.blocks[(size_t)j].idx - (long long)ab.idx != D)
                return fail("busy", "audio streams were interrupted; try again", true);
            break;
        }
    }

    std::vector<float> x(a.x.begin() + (ia - aFirst), a.x.begin() + (ia - aFirst + L));
    std::vector<float> y(b.x.begin() + (ia + D - M - bFirst), b.x.begin() + (ia + D + L + M - bFirst));
    const double ex = energy(x.data(), x.size());
    const double ey = energy(y.data() + M, (size_t)L);
    const double floorE = 1e-9 * (double)L;  // -90 dBFS
    if (ex < floorE) return fail("busy", "no signal on a", true);
    (void)ey;
    if (energy(y.data(), y.size()) < 1e-9 * (double)y.size()) return fail("busy", "no signal on b", true);

    std::vector<double> c = crossCorrelate(x, y);
    // normalise by the energy of the B window at each lag (prefix sums)
    std::vector<double> pre(y.size() + 1, 0.0);
    for (size_t i = 0; i < y.size(); i++) pre[i + 1] = pre[i] + (double)y[i] * y[i];
    std::vector<double> rho(c.size());
    size_t best = 0;
    for (size_t k = 0; k < c.size(); k++) {
        const double e = pre[k + (size_t)L] - pre[k];
        rho[k] = e > 0 ? c[k] / std::sqrt(e) : 0.0;
        if (std::fabs(rho[k]) > std::fabs(rho[best])) best = k;
    }
    const long long excl = std::max<long long>(8, std::llround(0.001 * sr));
    double second = 0;
    for (size_t k = 0; k < rho.size(); k++)
        if (std::llabs((long long)k - (long long)best) > excl) second = std::max(second, std::fabs(rho[k]));

    // exact normalised correlation at the peak, in the time domain
    double xy = 0;
    for (long long n = 0; n < L; n++) xy += (double)x[(size_t)n] * y[(size_t)(n + (long long)best)];
    const double eyb = pre[best + (size_t)L] - pre[best];
    r.ok = true;
    r.delaySamples = (long long)best - M;
    r.correlationPeak = eyb > 0 ? std::min(1.0, std::fabs(xy) / std::sqrt(ex * eyb)) : 0.0;
    r.polarity = xy < 0 ? -1 : 1;
    r.ambiguity = std::fabs(rho[best]) > 0 ? std::min(1.0, second / std::fabs(rho[best])) : 1.0;
    r.seconds = (double)L / sr;
    return r;
}

}  // namespace liveprobe
