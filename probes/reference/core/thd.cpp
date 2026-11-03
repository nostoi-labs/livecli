// SPDX-License-Identifier: MIT
#include "thd.h"

#include <algorithm>
#include <cmath>
#include <complex>

#ifdef __APPLE__
#include <Accelerate/Accelerate.h>
#endif

namespace liveprobe {

namespace {

constexpr int kHalfLobe = 10;  // bins summed each side of a component (the 7-term window's main lobe is ±8)

/// 7-term Blackman-Harris: sidelobes below -180 dB, so a clean sine shows the real floor.
std::vector<double> window(int n, double& sumW2) {
    static const double a[7] = {0.27105140069342, 0.43329793923448, 0.21812299954311, 0.06592544638803,
                                0.01081174209837, 0.00077658482522, 0.00001388721735};
    std::vector<double> w((size_t)n);
    sumW2 = 0;
    for (int i = 0; i < n; i++) {
        double v = 0, sgn = 1;
        for (int k = 0; k < 7; k++, sgn = -sgn) v += sgn * a[k] * std::cos(2.0 * M_PI * k * i / n);
        w[(size_t)i] = v;
        sumW2 += v * v;
    }
    return w;
}

/// One-sided power of a windowed real frame, unnormalised: |X_k|² for k = 0 … n/2.
void framePower(const std::vector<double>& in, std::vector<double>& pw) {
    const size_t n = in.size();
    pw.assign(n / 2 + 1, 0.0);
#ifdef __APPLE__
    int log2n = 0;
    while ((size_t)1 << log2n < n) log2n++;
    std::vector<double> re(n / 2), im(n / 2);
    DSPDoubleSplitComplex z{re.data(), im.data()};
    vDSP_ctozD((const DSPDoubleComplex*)in.data(), 2, &z, 1, n / 2);
    FFTSetupD setup = vDSP_create_fftsetupD(log2n, kFFTRadix2);
    vDSP_fft_zripD(setup, &z, 1, log2n, kFFTDirection_Forward);
    vDSP_destroy_fftsetupD(setup);
    // zrip output is 2·X; DC and Nyquist packed in bin 0
    pw[0] = 0.25 * re[0] * re[0];
    pw[n / 2] = 0.25 * im[0] * im[0];
    for (size_t k = 1; k < n / 2; k++) pw[k] = 0.25 * (re[k] * re[k] + im[k] * im[k]);
#else
    using cd = std::complex<double>;
    std::vector<cd> a(in.begin(), in.end());
    for (size_t i = 1, j = 0; i < n; i++) {
        size_t bit = n >> 1;
        for (; j & bit; bit >>= 1) j ^= bit;
        j ^= bit;
        if (i < j) std::swap(a[i], a[j]);
    }
    for (size_t len = 2; len <= n; len <<= 1) {
        const double ang = -2 * M_PI / len;
        const cd wl(std::cos(ang), std::sin(ang));
        for (size_t i = 0; i < n; i += len) {
            cd w(1);
            for (size_t j = 0; j < len / 2; j++, w *= wl) {
                cd u = a[i + j], v = a[i + j + len / 2] * w;
                a[i + j] = u + v;
                a[i + j + len / 2] = u - v;
            }
        }
    }
    for (size_t k = 0; k <= n / 2; k++) pw[k] = std::norm(a[k]);
#endif
}

double sumBins(const std::vector<double>& p, long lo, long hi) {
    lo = std::max(1L, lo);
    hi = std::min((long)p.size() - 1, hi);
    double s = 0;
    for (long k = lo; k <= hi; k++) s += p[(size_t)k];
    return s;
}

}  // namespace

int thdFftSize(double sr) {
    int n = 1 << 14;
    while (n < sr * 1.36) n <<= 1;  // 65536 at 44.1/48 kHz, 131072 at 88.2/96 kHz
    return n;
}

ThdResult measureThd(const float* x, size_t len, double sr, double freqHint) {
    ThdResult r;
    auto fail = [&](const char* code, const std::string& msg) {
        r.ok = false;
        r.code = code;
        r.error = msg;
        return r;
    };
    const int n = thdFftSize(sr);
    r.fftSize = n;
    if (len < (size_t)n) return fail("busy", "not enough audio (" + std::to_string(len) + " of " + std::to_string(n) + " samples)");
    if (freqHint > 0 && (freqHint < 20 || freqHint > 0.45 * sr)) return fail("bad_request", "freq must be 20 Hz … 0.45 × sample rate");
    for (size_t i = 0; i < len; i++)
        if (std::fabs(x[i]) >= 0.98855) { r.clipping = true; break; }  // -0.1 dBFS

    // Welch: frames of n with 50 % overlap, power averaged
    double sumW2 = 0;
    const std::vector<double> w = window(n, sumW2);
    std::vector<double> P((size_t)n / 2 + 1, 0.0), pw, frame((size_t)n);
    int frames = 0;
    for (size_t start = 0; start + (size_t)n <= len; start += (size_t)n / 2, frames++) {
        for (int i = 0; i < n; i++) frame[(size_t)i] = x[start + (size_t)i] * w[(size_t)i];
        framePower(frame, pw);
        for (size_t k = 0; k < P.size(); k++) P[k] += pw[k];
    }
    // normalise: summing a sine's lobe gives its mean square A²/2
    const double norm = 2.0 / ((double)n * sumW2 * frames);
    for (double& v : P) v *= norm;
    r.frames = frames;
    const double bin = sr / n;

    // fundamental: strongest bin near the hint (±2 %, at least ±5 bins) or in 20 Hz … 0.45·sr
    long lo = (long)std::ceil(20.0 / bin), hi = (long)std::floor(0.45 * sr / bin);
    if (freqHint > 0) {
        const double tol = std::max(5.0 * bin, 0.02 * freqHint);
        lo = std::max(lo, (long)std::floor((freqHint - tol) / bin));
        hi = std::min(hi, (long)std::ceil((freqHint + tol) / bin));
    }
    long k0 = lo;
    for (long k = lo; k <= hi; k++) if (P[(size_t)k] > P[(size_t)k0]) k0 = k;
    // sub-bin: power-weighted centroid over the main lobe
    double num = 0, den = 0;
    for (long k = k0 - 8; k <= k0 + 8; k++) {
        if (k < 1 || k >= (long)P.size()) continue;
        num += k * P[(size_t)k];
        den += P[(size_t)k];
    }
    const double kf = den > 0 ? num / den : (double)k0;
    r.freq = kf * bin;
    const double pf = sumBins(P, k0 - kHalfLobe, k0 + kHalfLobe);
    r.fundamentalDb = pf > 0 ? 10 * std::log10(2.0 * pf) : -300;  // peak amplitude: A = sqrt(2·mean square)
    if (r.fundamentalDb < -90) return fail("busy", "no sine found (fundamental below -90 dBFS)");

    // band 20 Hz … 20 kHz (and below Nyquist), DC excluded
    const long bLo = (long)std::ceil(20.0 / bin), bHi = (long)std::min((double)P.size() - 1, std::floor(std::min(20000.0, 0.5 * sr) / bin));
    const double total = sumBins(P, bLo, bHi);
    if (pf < 0.5 * total) return fail("busy", "no stable sine near the requested frequency (fundamental holds " +
                                                   std::to_string((int)std::lround(100 * pf / total)) + " % of the power)");

    // harmonics 2…10 inside the band
    double ph = 0;
    std::vector<bool> used(P.size(), false);
    for (long k = k0 - kHalfLobe; k <= k0 + kHalfLobe; k++) if (k >= 0 && k < (long)used.size()) used[(size_t)k] = true;
    for (int h = 2; h <= 10; h++) {
        const double fh = h * r.freq;
        if (fh > 20000.0 || fh >= 0.5 * sr - kHalfLobe * bin) { r.harmonicsDb.push_back(NAN); continue; }
        const long kh = std::lround(h * kf);
        const double p = sumBins(P, kh - kHalfLobe, kh + kHalfLobe);
        for (long k = kh - kHalfLobe; k <= kh + kHalfLobe; k++) if (k >= 0 && k < (long)used.size()) used[(size_t)k] = true;
        ph += p;
        r.harmonicsDb.push_back(p > 0 ? 10 * std::log10(p / pf) : -300);
    }
    r.thdPct = 100.0 * std::sqrt(ph / pf);
    r.thdDb = ph > 0 ? 10 * std::log10(ph / pf) : -300;

    // THD+N: everything in the band except the fundamental (±1/2 octave notch, §5.13)
    const long nLo = (long)std::floor(r.freq / M_SQRT2 / bin), nHi = (long)std::ceil(r.freq * M_SQRT2 / bin);
    double pn = 0, noise = 0;
    for (long k = bLo; k <= bHi; k++) {
        const bool notch = k >= nLo && k <= nHi;
        if (!notch) pn += P[(size_t)k];
        if (!notch && !used[(size_t)k]) noise += P[(size_t)k];
    }
    r.thdNPct = 100.0 * std::sqrt(pn / pf);
    r.thdNDb = pn > 0 ? 10 * std::log10(pn / pf) : -300;
    r.noiseDb = noise > 0 ? 10 * std::log10(noise) : -300;
    r.snrDb = noise > 0 ? 10 * std::log10(pf / noise) : 300;
    r.ok = true;
    return r;
}

}  // namespace liveprobe
