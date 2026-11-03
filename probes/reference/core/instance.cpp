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

void Instance::pushAudio(const float* l, const float* r, int frames, double sampleRate, int64_t samplePos, double t) {
    if (frames <= 0 || !l) return;
    BlockInfo bi;
    bi.frames = frames;
    bi.channels = r ? 2 : 1;
    bi.t = t >= 0 ? t : monoNow();
    bi.samplePos = samplePos;
    bi.sampleRate = sampleRate;
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

void Instance::reset() {
    std::lock_guard<std::mutex> lk(m_);
    engine_->reset();
    histHead_ = histCount_ = 0;
    secOpen_ = false;
    resetT_ = monoNow();
}

}  // namespace liveprobe
