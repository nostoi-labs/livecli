// SPDX-License-Identifier: MIT
#include "onsets.h"

#include <algorithm>
#include <cmath>

namespace liveprobe {

namespace {
constexpr size_t kHist = 1 << 14;   // 170 ms at 96 kHz: enough for look-back + peak window
constexpr size_t kBlocks = 1024;
}  // namespace

void OnsetDetector::prepare(double sr) {
    sr_ = sr;
    // 2nd-order Butterworth high-pass at 40 Hz (RBJ)
    const double w = 2 * M_PI * 40.0 / sr, c = std::cos(w), al = std::sin(w) / (2 * M_SQRT1_2);
    const double a0 = 1 + al;
    Biquad hp;
    hp.b0 = (1 + c) / 2 / a0; hp.b1 = -(1 + c) / a0; hp.b2 = (1 + c) / 2 / a0;
    hp.a1 = -2 * c / a0; hp.a2 = (1 - al) / a0;
    hpL_ = hpR_ = hp;
    hist_.assign(kHist, 0.f);
    mask_ = kHist - 1;
    idx_ = 0;
    blocks_.assign(kBlocks, Block());
    blockHead_ = blockCount_ = 0;
    env_ = floor_ = 0;
    relFast_ = std::exp(-1.0 / (0.001 * sr));    // ~1 ms envelope release
    floorDown_ = std::exp(-1.0 / (0.050 * sr));  // the floor follows quiet parts quickly…
    floorUp_ = std::exp(-1.0 / (0.500 * sr));    // …and loud ones slowly
    collecting_ = false;
    // start in refractory: no onset in the first 50 ms, while the floor is still unknown (the tail of a
    // transient that was already sounding when the collection started would read as an onset)
    haveLast_ = true;
    lastOnset_ = 0;
    win_ = std::lround(0.005 * sr);              // peak search after the trigger
    look_ = std::lround(0.002 * sr);             // half-peak crossing may precede the trigger
    refractory_ = std::lround(0.050 * sr);
}

void OnsetDetector::setThresholdDb(double db) { thrLin_ = std::pow(10.0, db / 20.0); }

void OnsetDetector::process(const float* l, const float* r, int n, const BlockInfo& bi, std::vector<Onset>& out) {
    if (bi.sampleRate != sr_) prepare(bi.sampleRate);
    blocks_[blockHead_] = Block{idx_, bi};
    blockHead_ = (blockHead_ + 1) % kBlocks;
    blockCount_ = std::min(blockCount_ + 1, kBlocks);
    const bool stereo = bi.channels > 1;
    for (int i = 0; i < n; i++, idx_++) {
        const double hl = hpL_.run(l[i]);
        const double a = stereo ? std::max(std::fabs(hl), std::fabs(hpR_.run(r[i]))) : std::fabs(hl);
        hist_[idx_ & mask_] = (float)a;
        env_ = std::max(a, env_ * relFast_);
        if (collecting_) {
            if ((long)(idx_ - trigger_) >= win_) finish(out);
            continue;
        }
        const bool ready = !haveLast_ || (long)(idx_ - lastOnset_) >= refractory_;
        if (ready && env_ > thrLin_ && env_ > 4.0 * floor_) {  // above the threshold and 12 dB over the floor
            collecting_ = true;
            trigger_ = idx_;
        }
        floor_ = env_ < floor_ ? env_ + (floor_ - env_) * floorDown_ : env_ + (floor_ - env_) * floorUp_;
    }
}

void OnsetDetector::finish(std::vector<Onset>& out) {
    collecting_ = false;
    // peak of the transient in [trigger, trigger + win]
    uint64_t start = trigger_ >= (uint64_t)look_ ? trigger_ - look_ : 0;
    if (haveLast_) start = std::max(start, lastOnset_ + 1);
    float peak = 0;
    uint64_t at = trigger_;
    for (uint64_t k = trigger_; k < idx_; k++)
        if (hist_[k & mask_] > peak) { peak = hist_[k & mask_]; at = k; }
    // first sample at or above half the peak (-6 dB), with linear interpolation
    const float half = peak * 0.5f;
    double pos = (double)at;
    for (uint64_t k = start; k <= at; k++) {
        if (hist_[k & mask_] >= half) {
            pos = (double)k;
            if (k > start) {
                const float a0 = hist_[(k - 1) & mask_], a1 = hist_[k & mask_];
                if (a1 > a0) pos = (double)(k - 1) + (half - a0) / (a1 - a0);
            }
            break;
        }
    }
    lastOnset_ = (uint64_t)std::floor(pos);
    haveLast_ = true;
    Onset o;
    o.levelDb = peak > 0 ? 20.0 * std::log10(peak) : -150.0;
    bool playing = false;
    if (locate(pos, o, playing) && playing) out.push_back(o);
    else stoppedOnsets++;
}

/// Song position of stream position `pos` from the block that holds it.
bool OnsetDetector::locate(double pos, Onset& o, bool& playing) const {
    for (size_t i = 0; i < blockCount_; i++) {
        const Block& b = blocks_[(blockHead_ + kBlocks - 1 - i) % kBlocks];
        if ((double)b.idx > pos) continue;
        const double off = (pos - (double)b.idx) / sr_;  // seconds into the block
        const Transport& tr = b.info.transport;
        o.t = b.info.t + off;
        playing = (tr.flags & Transport::kPlaying) && (tr.flags & Transport::kMusicValid) && (tr.flags & Transport::kTempoValid) &&
                  tr.tempo > 0;
        if (!playing) return true;
        o.tempo = tr.tempo;
        o.ppq = tr.ppq + off * tr.tempo / 60.0;
        // a loop that wraps inside the block
        if ((tr.flags & Transport::kCycleActive) && tr.cycleEnd > tr.cycleStart && tr.ppq < tr.cycleEnd && o.ppq >= tr.cycleEnd)
            o.ppq -= tr.cycleEnd - tr.cycleStart;
        return true;
    }
    return false;
}

}  // namespace liveprobe
