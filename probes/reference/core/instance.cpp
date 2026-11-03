// SPDX-License-Identifier: MIT
#include "instance.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>

namespace liveprobe {

namespace {
constexpr size_t kTicks = 2048;    // 10 ms ticks: 20 s (windows up to 10 s)
constexpr size_t kFrames = 1024;   // band frames (~20-40 s depending on the engine's hop)
constexpr size_t kHistory = 128;   // closed seconds (protocol: at least 120)
constexpr float kSilence = 1e-7f;  // -140 dBFS
constexpr double kIdleAfter = 3.5; // seconds of silence before the engine stops running
constexpr int kBassBands = 8;      // bands below ~113 Hz (upper edge of the 100.8 Hz band)

double dbAmp(double a) { return a > 0 ? 20.0 * std::log10(a) : -INFINITY; }
double dbPow(double p) { return p > 0 ? 10.0 * std::log10(p) : -INFINITY; }
}  // namespace

double monoNow() {
#ifdef __APPLE__
    return (double)clock_gettime_nsec_np(CLOCK_UPTIME_RAW) * 1e-9;
#else
    timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return ts.tv_sec + ts.tv_nsec * 1e-9;
#endif
}

Instance::Instance(std::string u, uint64_t s) : uid(std::move(u)), serial(s) {
    id_.uid = uid;
    engine_ = product().makeEngine();
    engine_->onBandFrame = [this](const BandFrame& f) { onFrame(f, pieceEndT_); };
    ticks_.resize(kTicks);
    frames_.resize(kFrames);
    history_.resize(kHistory);
    bufL_.resize(AudioRing::kMaxChunk);
    bufR_.resize(AudioRing::kMaxChunk);
    resetT_ = monoNow();
}

Instance::~Instance() = default;

// ---------------------------------------------------------------- identity

void Instance::setProbeId(int v) {
    v = std::max(0, std::min(999, v));
    if (probeId_.exchange(v, std::memory_order_relaxed) != v) version_.fetch_add(1, std::memory_order_release);
}

void Instance::setTrack(bool hasName, const std::string& name, bool hasIndex, long long index, bool hasColor,
                        uint32_t rgb) {
    std::lock_guard<std::mutex> lk(idm_);
    rgb &= 0xFFFFFF;
    if (id_.hasTrack == hasName && (!hasName || id_.track == name) && id_.hasIndex == hasIndex &&
        (!hasIndex || id_.trackIndex == index) && id_.hasColor == hasColor && (!hasColor || id_.color == rgb))
        return;  // nothing changed: no instance_changed event
    id_.hasTrack = hasName;
    id_.track = hasName ? name : std::string();
    id_.hasIndex = hasIndex;
    id_.trackIndex = index;
    id_.hasColor = hasColor;
    id_.color = rgb & 0xFFFFFF;
    version_.fetch_add(1, std::memory_order_release);
}

void Instance::setLabel(const std::string& label) {
    std::lock_guard<std::mutex> lk(idm_);
    if (id_.label == label.substr(0, 256)) return;
    id_.label = label.substr(0, 256);
    version_.fetch_add(1, std::memory_order_release);
}

void Instance::setFormat(int channels, double sampleRate) {
    std::lock_guard<std::mutex> lk(idm_);
    if (id_.channels == channels && id_.sampleRate == sampleRate) return;
    id_.channels = channels;
    id_.sampleRate = sampleRate;
    channels_.store(channels);
    version_.fetch_add(1, std::memory_order_release);
}

Identity Instance::identity() const {
    std::lock_guard<std::mutex> lk(idm_);
    Identity c = id_;
    c.probeId = probeId_.load(std::memory_order_relaxed);
    return c;
}

// ---------------------------------------------------------------- audio thread

void Instance::pushAudio(const float* l, const float* r, int frames, double sampleRate, int64_t samplePos, double t,
                         const Transport* tr) {
    if (frames <= 0 || !l) return;
    BlockInfo bi;
    bi.frames = frames;
    bi.channels = r ? 2 : 1;
    bi.t = t >= 0 ? t : monoNow();
    bi.samplePos = samplePos;
    bi.sampleRate = sampleRate;
    if (tr) bi.transport = *tr;
    lastFlags_.store(bi.transport.flags, std::memory_order_relaxed);
    ring_.write(l, r, frames, bi);
    lastAudioT_.store(bi.t, std::memory_order_relaxed);
}

// ---------------------------------------------------------------- analysis thread

void Instance::prepare(double sr) {
    sr_ = sr;
    tickLen_ = std::max(1, (int)std::lround(sr / 100.0));
    engine_->prepare(sr);
    cur_ = Tick();
    tickHead_ = tickCount_ = 0;
    frameHead_ = frameCount_ = 0;
    lastFrameT_ = -1;
    silentRun_ = 0;
}

int Instance::analyze() {
    std::lock_guard<std::mutex> lk(m_);
    BlockInfo bi;
    int total = 0;
    while (ring_.read(bi, bufL_.data(), bufR_.data())) {
        if (bi.sampleRate != sr_ && bi.sampleRate > 0) prepare(bi.sampleRate);
        if (bi.channels != engineChannels_) engine_->setChannels(engineChannels_ = bi.channels);
        if (capUntil_ > 0) capture(bufL_.data(), bufR_.data(), bi);
        if (onsetUntil_ > 0) {
            if (bi.t > onsetUntil_ + 5.0) {  // nobody is waiting: stop and free
                onsetUntil_ = -1;
                std::vector<Onset>().swap(onsets_);
                playRuns_.clear();
            } else {
                onsetDet_.process(bufL_.data(), bufR_.data(), bi.frames, bi, onsets_);
                if (onsets_.size() > 8192) onsets_.erase(onsets_.begin(), onsets_.begin() + 4096);
                const uint32_t need = Transport::kPlaying | Transport::kMusicValid | Transport::kTempoValid;
                if ((bi.transport.flags & need) == need) {
                    const double end = bi.t + bi.frames / bi.sampleRate;
                    // consecutive blocks join one run (allow the timestamp jitter of the audio callback)
                    if (!playRuns_.empty() && bi.t - playRuns_.back().second < 0.05) playRuns_.back().second = end;
                    else playRuns_.push_back({bi.t, end});
                    if (playRuns_.size() > 256) playRuns_.erase(playRuns_.begin());
                }
            }
        }
        analyzedT_ = bi.t + bi.frames / bi.sampleRate;
        int off = 0;
        while (off < bi.frames) {
            int n = std::min(bi.frames - off, tickLen_ - cur_.n);
            const double t = bi.t + off / sr_;
            // split at whole seconds of the server clock, so the 1-second summaries of two instances
            // that see the same samples (pre/post on one track) hold the same samples, to a few frames
            const int toSecond = (int)std::ceil((std::floor(t) + 1.0 - t) * sr_);
            if (toSecond >= 1 && toSecond < n) n = toSecond;
            processPiece(bufL_.data() + off, bufR_.data() + off, n, t);
            off += n;
        }
        total += bi.frames;
    }
    return total;
}

void Instance::processPiece(const float* l, const float* r, int n, double t) {
    double s = std::floor(t);
    if (!secOpen_) {
        sec_ = Second();
        sec_.t = s;
        secOpen_ = true;
    } else if (s > sec_.t) {
        closeSecond();
        sec_ = Second();
        sec_.t = s;
        secOpen_ = true;
    }
    if (cur_.n == 0) cur_.t = t;
    float pl = 0, pr = 0;
    double ll = 0, rr = 0, lr = 0;
    for (int i = 0; i < n; i++) {
        float a = l[i], b = r[i];
        float aa = std::fabs(a), bb = std::fabs(b);
        if (aa > pl) pl = aa;
        if (bb > pr) pr = bb;
        ll += (double)a * a;
        rr += (double)b * b;
        lr += (double)a * b;
    }
    cur_.peak[0] = std::max(cur_.peak[0], pl);
    cur_.peak[1] = std::max(cur_.peak[1], pr);
    cur_.ll += ll; cur_.rr += rr; cur_.lr += lr; cur_.n += n;
    sec_.peak[0] = std::max(sec_.peak[0], pl);
    sec_.peak[1] = std::max(sec_.peak[1], pr);
    sec_.ll += ll; sec_.rr += rr; sec_.lr += lr; sec_.n += n;

    bool silent = std::max(pl, pr) < kSilence;
    silentRun_ = silent ? silentRun_ + n : 0;
    pieceEndT_ = t + n / sr_;
    if (silent && silentRun_ > (long)(kIdleAfter * sr_)) {
        // idle: the engine has seen enough silence to read silence; skip it and log empty bands
        if (engine_->hasBands()) {
            BandFrame z;
            std::fill(z.mid, z.mid + kBands, 0.f);
            std::fill(z.side, z.side + kBands, 0.f);
            onFrame(z, pieceEndT_);
        }
    } else {
        engine_->process(l, r, n);
    }

    if (cur_.n >= tickLen_) {
        ticks_[tickHead_] = cur_;
        tickHead_ = (tickHead_ + 1) % kTicks;
        tickCount_ = std::min(tickCount_ + 1, kTicks);
        cur_ = Tick();
    }
}

void Instance::onFrame(const BandFrame& f, double t) {
    double dur = lastFrameT_ < 0 ? 0.02 : std::max(0.0005, std::min(0.25, t - lastFrameT_));
    lastFrameT_ = t;
    Frame& fr = frames_[frameHead_];
    fr.t = t;
    fr.dur = dur;
    fr.b = f;
    frameHead_ = (frameHead_ + 1) % kFrames;
    frameCount_ = std::min(frameCount_ + 1, kFrames);
    if (secOpen_) {
        for (int k = 0; k < kBands; k++) {
            sec_.mid[k] += f.mid[k] * dur;
            sec_.side[k] += f.side[k] * dur;
        }
        sec_.bandDur += dur;
    }
}

void Instance::closeSecond() {
    LoudnessValues lv;
    engine_->loudness(lv, false);
    sec_.lufsM = lv.lufsM;
    sec_.lufsS = lv.lufsS;
    sec_.tp = engine_->takeTruePeak();
    if (sec_.bandDur > 0) {
        for (int k = 0; k < kBands; k++) {
            sec_.mid[k] /= sec_.bandDur;
            sec_.side[k] /= sec_.bandDur;
        }
    }
    history_[histHead_] = sec_;
    histHead_ = (histHead_ + 1) % kHistory;
    histCount_ = std::min(histCount_ + 1, kHistory);
    secOpen_ = false;
}

// ---------------------------------------------------------------- readouts (server thread)

void Instance::windowStats(double windowS, float peak[2], double& ll, double& rr, double& lr, long& n) const {
    peak[0] = cur_.peak[0];
    peak[1] = cur_.peak[1];
    ll = cur_.ll; rr = cur_.rr; lr = cur_.lr; n = cur_.n;
    if (sr_ <= 0) return;
    double tickS = tickLen_ / sr_;
    size_t want = (size_t)std::ceil(windowS / tickS - 1e-9);
    if (cur_.n > 0 && want > 0) want--;  // the partial tick counts as one
    want = std::min(want, tickCount_);
    for (size_t i = 0; i < want; i++) {
        const Tick& k = ticks_[(tickHead_ + kTicks - 1 - i) % kTicks];
        peak[0] = std::max(peak[0], k.peak[0]);
        peak[1] = std::max(peak[1], k.peak[1]);
        ll += k.ll; rr += k.rr; lr += k.lr; n += k.n;
    }
}

bool Instance::hasSignal() const {
    float pk[2];
    double ll, rr, lr;
    long n;
    windowStats(1.0, pk, ll, rr, lr, n);
    return n > 0 && (ll + rr) / (2.0 * n) > 1e-9;  // -90 dBFS
}

void Instance::writeIdentity(JWriter& w, double now) {
    Identity id = identity();
    bool active = now - lastAudioT_.load(std::memory_order_relaxed) < 1.0;
    bool signal = false;
    {
        std::lock_guard<std::mutex> lk(m_);
        signal = active && hasSignal();
    }
    w.beginObject();
    w.key("uid").str(id.uid);
    w.key("probe_id").integer(id.probeId);
    w.key("track");
    if (id.hasTrack) w.str(id.track); else w.null();
    w.key("track_index");
    if (id.hasIndex) w.integer(id.trackIndex); else w.null();
    w.key("track_color");
    if (id.hasColor) {
        char b[8];
        snprintf(b, sizeof b, "#%06X", id.color & 0xFFFFFF);
        w.str(b);
    } else {
        w.null();
    }
    w.key("label").str(id.label);
    w.key("channels").integer(id.channels);
    w.key("sample_rate").number(id.sampleRate, 1);
    w.key("latency_samples").integer(0);
    w.key("active").boolean(active);
    w.key("signal").boolean(signal);
    w.key("dropped_blocks").integer((long long)ring_.dropped());
    w.key("pulsing").boolean(pulsing());
    w.endObject();
}

void Instance::writeSnapshot(JWriter& w, double windowS, double now, const std::vector<std::string>* fields) {
    auto want = [&](const char* k) {
        if (!fields) return true;
        for (auto& f : *fields) if (f == k) return true;
        return false;
    };
    const int ch = channels_.load();
    std::lock_guard<std::mutex> lk(m_);
    float pk[2];
    double ll, rr, lr;
    long n;
    windowStats(windowS, pk, ll, rr, lr, n);
    const double msL = n ? ll / n : 0, msR = n ? rr / n : 0;

    w.beginObject();
    w.key("uid").str(uid);
    w.key("t").number(now, 3);
    w.key("window_s").number(windowS, 2);
    if (want("peak_db")) {
        w.key("peak_db").beginArray().db(dbAmp(pk[0]));
        if (ch > 1) w.db(dbAmp(pk[1]));
        w.endArray();
    }
    if (want("rms_db")) {
        w.key("rms_db").beginArray().db(dbPow(msL));
        if (ch > 1) w.db(dbPow(msR));
        w.endArray();
    }
    const double msAll = ch > 1 ? (msL + msR) / 2 : msL;
    if (want("crest_db")) {
        w.key("crest_db");
        double p = std::max(pk[0], ch > 1 ? pk[1] : 0.f);
        if (p > 0 && msAll > 0) w.number(dbAmp(p) - dbPow(msAll), 1); else w.null();
    }
    if (ch > 1) {
        double mm = n ? (ll + rr + 2 * lr) / (4.0 * n) : 0, ss = n ? (ll + rr - 2 * lr) / (4.0 * n) : 0;
        if (mm < 0) mm = 0;
        if (ss < 0) ss = 0;
        if (want("correlation")) {
            w.key("correlation");
            if (ll > 1e-20 && rr > 1e-20) w.number(std::max(-1.0, std::min(1.0, lr / std::sqrt(ll * rr))), 3);
            else w.null();
        }
        if (want("width")) {
            w.key("width");
            if (mm + ss > 1e-20) w.number(ss / (mm + ss), 3); else w.null();
        }
        if (want("mid_db")) w.key("mid_db").db(dbPow(mm));
        if (want("side_db")) w.key("side_db").db(dbPow(ss));
    }

    if (engine_->hasBands() && (want("bands") || want("bass_mono"))) {
        double mid[kBands] = {}, side[kBands] = {}, wsum = 0;
        if (frameCount_ > 0) {
            const double tref = frames_[(frameHead_ + kFrames - 1) % kFrames].t;
            const double tau = std::max(0.05, windowS);
            for (size_t i = 0; i < frameCount_; i++) {
                const Frame& f = frames_[(frameHead_ + kFrames - 1 - i) % kFrames];
                double age = tref - f.t;
                if (age > 6 * tau) break;
                double wt = f.dur * std::exp(-age / tau);
                for (int k = 0; k < kBands; k++) {
                    mid[k] += wt * f.b.mid[k];
                    side[k] += wt * f.b.side[k];
                }
                wsum += wt;
            }
            if (wsum > 0) for (int k = 0; k < kBands; k++) { mid[k] /= wsum; side[k] /= wsum; }
        }
        if (want("bands")) {
            w.key("bands").beginObject();
            w.key("centers").beginArray();
            for (int k = 0; k < kBands; k++) w.number(bandCenter(k), 1);
            w.endArray();
            w.key("mid").beginArray();
            for (int k = 0; k < kBands; k++) w.db(dbPow(mid[k]));
            w.endArray();
            if (ch > 1) {
                w.key("side").beginArray();
                for (int k = 0; k < kBands; k++) w.db(dbPow(side[k]));
                w.endArray();
            }
            w.endObject();
        }
        if (want("bass_mono") && ch > 1) {
            double m = 0, s = 0;
            for (int k = 0; k < kBassBands; k++) { m += mid[k]; s += side[k]; }
            w.key("bass_mono");
            if (m + s > 1e-14) w.number((m - s) / (m + s), 3); else w.null();
        }
    }

    LoudnessValues lv;
    engine_->loudness(lv, want("lufs_i") || want("lra") || want("plr"));
    if (!std::isnan(lv.truePeakMax) && want("true_peak_dbtp")) w.key("true_peak_dbtp").db(lv.truePeakMax);
    if (!std::isnan(lv.lufsM) && want("lufs_m")) w.key("lufs_m").db(lv.lufsM);
    if (!std::isnan(lv.lufsS) && want("lufs_s")) w.key("lufs_s").db(lv.lufsS);
    if (!std::isnan(lv.lufsI) && want("lufs_i")) w.key("lufs_i").db(lv.lufsI);
    if (!std::isnan(lv.lra) && want("lra")) w.key("lra").number(lv.lra, 1);
    if (!std::isnan(lv.truePeakMax) && !std::isnan(lv.lufsI) && want("plr")) {
        w.key("plr");
        if (std::isfinite(lv.truePeakMax) && std::isfinite(lv.lufsI) && lv.lufsI > -150) w.number(lv.truePeakMax - lv.lufsI, 1);
        else w.null();
    }
    if (want("since_reset_s")) w.key("since_reset_s").number(now - resetT_, 1);
    w.endObject();
}

void Instance::writeHistory(JWriter& w, int seconds, double now) {
    const int ch = channels_.load();
    std::lock_guard<std::mutex> lk(m_);
    const double from = std::floor(now) - seconds;
    w.beginArray();
    for (size_t i = 0; i < histCount_; i++) {
        const Second& s = history_[(histHead_ + kHistory - histCount_ + i) % kHistory];
        if (s.t < from) continue;
        w.beginObject();
        w.key("t").number(s.t, 1);
        double msL = s.n ? s.ll / s.n : 0, msR = s.n ? s.rr / s.n : 0;
        w.key("peak_db").beginArray().db(dbAmp(s.peak[0]));
        if (ch > 1) w.db(dbAmp(s.peak[1]));
        w.endArray();
        w.key("rms_db").beginArray().db(dbPow(msL));
        if (ch > 1) w.db(dbPow(msR));
        w.endArray();
        if (!std::isnan(s.lufsM)) w.key("lufs_m").db(s.lufsM);
        if (!std::isnan(s.lufsS)) w.key("lufs_s").db(s.lufsS);
        if (!std::isnan(s.tp)) w.key("true_peak_dbtp").db(s.tp);
        if (ch > 1) {
            w.key("correlation");
            if (s.ll > 1e-20 && s.rr > 1e-20) w.number(std::max(-1.0, std::min(1.0, s.lr / std::sqrt(s.ll * s.rr))), 3);
            else w.null();
        }
        if (engine_->hasBands()) {
            w.key("bands_mid").beginArray();
            for (int k = 0; k < kBands; k++) w.db(dbPow(s.mid[k]));
            w.endArray();
            if (ch > 1) {
                w.key("bands_side").beginArray();
                for (int k = 0; k < kBands; k++) w.db(dbPow(s.side[k]));
                w.endArray();
            }
        }
        w.endObject();
    }
    w.endArray();
}

std::vector<Instance::SecondBands> Instance::secondBands(int seconds, double now) {
    std::lock_guard<std::mutex> lk(m_);
    std::vector<SecondBands> out;
    const double from = std::floor(now) - seconds;
    for (size_t i = 0; i < histCount_; i++) {
        const Second& s = history_[(histHead_ + kHistory - histCount_ + i) % kHistory];
        if (s.t < from || s.bandDur <= 0) continue;
        SecondBands b;
        b.t = s.t;
        for (int k = 0; k < kBands; k++) b.mid[k] = (float)s.mid[k];
        out.push_back(b);
    }
    return out;
}

// ---------------------------------------------------------------- delay capture

namespace {
constexpr size_t kCapFrames = 1 << 20;      // 21.8 s at 48 kHz, 10.9 s at 96 kHz (4 MB, only while armed)
constexpr size_t kCapBlocks = 1 << 15;
}  // namespace

void Instance::armCapture(double now, double keepS) {
    std::lock_guard<std::mutex> lk(m_);
    if (cap_.empty()) {
        cap_.assign(kCapFrames, 0.f);
        capBlocks_.assign(kCapBlocks, CaptureBlock{0, 0, 0});
        capBlockHead_ = capBlockCount_ = 0;
        capRunStart_ = capWritten_;
        capDropped_ = ring_.dropped();
    }
    capUntil_ = std::max(capUntil_, now + keepS);
}

void Instance::capture(const float* l, const float* r, const BlockInfo& bi) {
    if (bi.t > capUntil_) {  // nobody asked for a while: free the memory
        std::vector<float>().swap(cap_);
        std::vector<CaptureBlock>().swap(capBlocks_);
        capUntil_ = -1;
        return;
    }
    const uint64_t d = ring_.dropped();
    if (d != capDropped_ || bi.sampleRate != capRate_) {  // a gap or a new rate: start a new run
        capDropped_ = d;
        capRate_ = bi.sampleRate;
        capRunStart_ = capWritten_;
        capBlockCount_ = 0;
    }
    const uint64_t mask = kCapFrames - 1;
    for (int i = 0; i < bi.frames; i++) cap_[(capWritten_ + i) & mask] = bi.channels > 1 ? 0.5f * (l[i] + r[i]) : l[i];
    capBlocks_[capBlockHead_] = CaptureBlock{capWritten_, bi.t, bi.frames};
    capBlockHead_ = (capBlockHead_ + 1) % kCapBlocks;
    capBlockCount_ = std::min(capBlockCount_ + 1, kCapBlocks);
    capWritten_ += (uint64_t)bi.frames;
}

bool Instance::copyCapture(CaptureCopy& out) {
    std::lock_guard<std::mutex> lk(m_);
    if (cap_.empty() || capBlockCount_ == 0) return false;
    uint64_t first = std::max(capRunStart_, capWritten_ > kCapFrames ? capWritten_ - kCapFrames : 0);
    // start at a block boundary that is still in the block ring
    out.blocks.clear();
    for (size_t i = 0; i < capBlockCount_; i++) {
        const CaptureBlock& b = capBlocks_[(capBlockHead_ + kCapBlocks - capBlockCount_ + i) % kCapBlocks];
        if (b.idx >= first) out.blocks.push_back(b);
    }
    if (out.blocks.empty()) return false;
    first = out.blocks.front().idx;
    const uint64_t n = capWritten_ - first, mask = kCapFrames - 1;
    out.x.resize((size_t)n);
    for (uint64_t i = 0; i < n; i++) out.x[(size_t)i] = cap_[(first + i) & mask];
    out.first = first;
    out.sampleRate = capRate_;
    return true;
}

// ---------------------------------------------------------------- pulse

void Instance::startPulse(const PulseRequest& r) {
    std::lock_guard<std::mutex> lk(pulseWriteM_);
    pulseSeq_.fetch_add(1, std::memory_order_acq_rel);  // odd: being written
    cmdStop_.store(false, std::memory_order_relaxed);
    cmdEvery_.store(r.every, std::memory_order_relaxed);
    cmdOffset_.store(r.offset, std::memory_order_relaxed);
    cmdLevel_.store(std::pow(10.0, std::min(r.levelDb, kPulseMaxDb) / 20.0), std::memory_order_relaxed);
    cmdClick_.store(r.click, std::memory_order_relaxed);
    cmdReplace_.store(r.replace, std::memory_order_relaxed);
    cmdSeconds_.store(std::max(0.0, std::min(10.0, r.seconds)), std::memory_order_relaxed);
    cmdUntil_.store(r.untilT, std::memory_order_relaxed);
    pulseSeq_.fetch_add(1, std::memory_order_release);  // even: complete
}

void Instance::stopPulse() {
    std::lock_guard<std::mutex> lk(pulseWriteM_);
    pulseSeq_.fetch_add(1, std::memory_order_acq_rel);
    cmdStop_.store(true, std::memory_order_relaxed);
    pulseSeq_.fetch_add(1, std::memory_order_release);
}

bool Instance::renderPulse(float* const* out, int channels, int n, const Transport& tr, double sr, double t) {
    // pick up a new command (seqlock: retry on the next block if it is being written)
    const uint32_t s1 = pulseSeq_.load(std::memory_order_acquire);
    if (s1 != pulseSeen_ && (s1 & 1u) == 0) {
        const bool stop = cmdStop_.load(std::memory_order_relaxed);
        const double every = cmdEvery_.load(std::memory_order_relaxed), offset = cmdOffset_.load(std::memory_order_relaxed);
        const double level = cmdLevel_.load(std::memory_order_relaxed), seconds = cmdSeconds_.load(std::memory_order_relaxed);
        const double until = cmdUntil_.load(std::memory_order_relaxed);
        const bool click = cmdClick_.load(std::memory_order_relaxed), replace = cmdReplace_.load(std::memory_order_relaxed);
        std::atomic_thread_fence(std::memory_order_acquire);
        if (pulseSeq_.load(std::memory_order_relaxed) == s1) {
            pulseSeen_ = s1;
            pActive_ = !stop;
            pEvery_ = every; pOffset_ = offset; pLevel_ = std::min(level, 0.5011872336272722);  // -6 dBFS, whatever arrived
            pClick_ = click; pReplace_ = replace; pUntil_ = until;
            pRemaining_ = (long long)(seconds * sr);
            pPlayed_ = false;
            pBurstLeft_ = 0;
            pBurstLen_ = std::max(2, (int)std::lround(0.001 * sr));
        }
    }
    if (!pActive_) {
        pulsing_.store(false, std::memory_order_relaxed);
        return false;
    }
    const uint32_t need = Transport::kPlaying | Transport::kMusicValid | Transport::kTempoValid;
    const bool playing = (tr.flags & need) == need && tr.tempo > 0 && sr > 0;
    // ends: duration, wall clock (also covers a device switched off: no process() calls), transport stopped
    if (pRemaining_ <= 0 || t > pUntil_ || (pPlayed_ && !playing)) {
        pActive_ = false;
        pulsing_.store(false, std::memory_order_relaxed);
        return false;
    }
    const long long left = pRemaining_;  // samples of the duration still to go, from this block's start
    pRemaining_ -= n;
    if (!playing) return false;  // waits for the transport (until the deadline)
    pPlayed_ = true;
    pulsing_.store(true, std::memory_order_relaxed);
    if (pReplace_)
        for (int c = 0; c < channels; c++) if (out[c]) std::fill(out[c], out[c] + n, 0.f);

    auto burstAt = [&](int s, int j) {  // sample j of the burst at block sample s
        const float v = pClick_ ? (float)(pLevel_ * 0.5 * (1.0 - std::cos(2.0 * M_PI * (j + 1) / (pBurstLen_ + 1)))) : (float)pLevel_;
        for (int c = 0; c < channels; c++) if (out[c]) out[c][s] += v;
    };
    // the rest of a click that started in the previous block
    int carried = 0;
    for (; carried < pBurstLeft_ && carried < n && carried < left; carried++) burstAt(carried, pBurstLen_ - pBurstLeft_ + carried);
    pBurstLeft_ -= carried;

    const double q = sr * 60.0 / tr.tempo;  // samples per quarter note
    auto place = [&](double p0, int from, int to) {  // song position p0 at block sample `from`
        double k = std::ceil((p0 - 0.5 / q - pOffset_) / pEvery_ - 1e-9);
        for (int guard = 0; guard < 4096; guard++, k += 1) {
            const double pos = k * pEvery_ + pOffset_;
            const long s = from + std::lround((pos - p0) * q);
            if (s >= to || s >= left) break;  // the duration ends to the sample
            if (s < from) continue;
            if (!pClick_) {
                burstAt((int)s, 0);
            } else {
                int j = 0;
                for (; j < pBurstLen_ && s + j < n; j++) burstAt((int)(s + j), j);
                pBurstLeft_ = pBurstLen_ - j;
            }
        }
    };
    const double endPpq = tr.ppq + n / q;
    if ((tr.flags & Transport::kCycleActive) && tr.cycleEnd > tr.cycleStart && tr.ppq < tr.cycleEnd && endPpq > tr.cycleEnd) {
        const int wrap = (int)std::min<long>(n, std::lround((tr.cycleEnd - tr.ppq) * q));
        place(tr.ppq, 0, wrap);
        place(tr.cycleStart, wrap, n);
    } else {
        place(tr.ppq, 0, n);
    }
    return true;
}

void Instance::armOnsets(double until, double thresholdDb) {
    std::lock_guard<std::mutex> lk(m_);
    if (onsetUntil_ < 0) {
        if (sr_ > 0) onsetDet_.prepare(sr_);  // fresh state: no stale look-back samples
        onsetThrDb_ = thresholdDb;
        onsets_.clear();
        playRuns_.clear();
    } else {
        onsetThrDb_ = std::min(onsetThrDb_, thresholdDb);
    }
    onsetDet_.setThresholdDb(onsetThrDb_);
    onsetUntil_ = std::max(onsetUntil_, until);
}

Instance::OnsetWindow Instance::onsetsBetween(double t0, double t1, double thresholdDb) {
    std::lock_guard<std::mutex> lk(m_);
    OnsetWindow w;
    w.sampleRate = sr_;
    w.covered = analyzedT_ >= t1;
    for (const Onset& o : onsets_)
        if (o.t >= t0 && o.t < t1 && o.levelDb >= thresholdDb) w.onsets.push_back(o);
    for (const auto& run : playRuns_)
        if (run.first < t1 && run.second > t0) w.played = true;
    return w;
}

double Instance::captureCapacityS() const {
    std::lock_guard<std::mutex> lk(m_);
    const double sr = sr_ > 0 ? sr_ : 48000.0;
    return kCapFrames / sr;
}

void Instance::reset() {
    std::lock_guard<std::mutex> lk(m_);
    engine_->reset();
    histHead_ = histCount_ = 0;
    secOpen_ = false;
    resetT_ = monoNow();
}

}  // namespace liveprobe
