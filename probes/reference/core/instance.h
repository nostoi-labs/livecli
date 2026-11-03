// SPDX-License-Identifier: MIT
// Live Probe core: one probe instance (identity, audio ring, analysis state, readouts).
#pragma once

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "engine.h"
#include "json.h"
#include "ring.h"

namespace liveprobe {

/// Server clock: monotonic seconds, shared by every instance of the process.
double monoNow();

struct Identity {
    std::string uid;
    int probeId = 0;
    bool hasTrack = false;
    std::string track;
    bool hasIndex = false;
    long long trackIndex = 0;
    bool hasColor = false;
    uint32_t color = 0;  // 0xRRGGBB
    std::string label;
    int channels = 2;
    double sampleRate = 48000;
};

class Instance {
public:
    Instance(std::string uid, uint64_t serial);
    ~Instance();

    const std::string uid;
    const uint64_t serial;

    // ---- identity (any thread) ----
    void setProbeId(int id);  // lock-free: also called from the audio thread
    int probeId() const { return probeId_.load(std::memory_order_relaxed); }
    void setTrack(bool hasName, const std::string& name, bool hasIndex, long long index, bool hasColor, uint32_t rgb);
    void setLabel(const std::string& label);
    void setFormat(int channels, double sampleRate);  // not from the audio thread
    Identity identity() const;
    /// Bumped on every identity change (probe id, track, label, format).
    uint32_t version() const { return version_.load(std::memory_order_acquire); }

    // ---- audio thread ----
    /// Only memcpy's into the ring. `r` null for mono. `t` < 0: now (tests pass their own clock).
    void pushAudio(const float* l, const float* r, int frames, double sampleRate, int64_t samplePos, double t = -1);

    // ---- analysis thread ----
    /// Drains the ring and runs the analysis. Returns frames processed.
    int analyze();

    // ---- server thread ----
    /// Identity fields of §3 plus active/signal.
    void writeIdentity(JWriter& w, double now);
    /// §4.1 snapshot. `fields` null = everything.
    void writeSnapshot(JWriter& w, double windowS, double now, const std::vector<std::string>* fields);
    /// §4.2 summaries of the last `seconds`, oldest first.
    void writeHistory(JWriter& w, int seconds, double now);
    /// Per-second mid band power (linear) keyed by whole second, for compare.
    struct SecondBands { double t; float mid[kBands]; };
    std::vector<SecondBands> secondBands(int seconds, double now);
    void reset();
    uint64_t dropped() const { return ring_.dropped(); }

    // ---- delay capture (mid signal, only while a client asks for it) ----
    struct CaptureBlock {
        uint64_t idx;   // index of the first frame in the capture stream
        double t;       // server clock at the start of the block (from the audio thread)
        int32_t frames;
    };
    struct CaptureCopy {
        double sampleRate = 0;
        std::vector<float> x;               // contiguous mid samples, oldest first
        uint64_t first = 0;                 // stream index of x[0]
        std::vector<CaptureBlock> blocks;   // blocks inside x, oldest first
    };
    /// Server thread: starts (or keeps) capturing for `keepS` seconds from `now`.
    void armCapture(double now, double keepS = 120.0);
    /// Server thread: the captured run (no gaps). False when nothing is captured yet.
    bool copyCapture(CaptureCopy& out);
    /// Capacity of the capture in seconds at the current rate (0 when unknown).
    double captureCapacityS() const;

private:
    struct Tick {
        double t = 0;
        float peak[2] = {0, 0};
        double ll = 0, rr = 0, lr = 0;
        int n = 0;
    };
    struct Frame {
        double t = 0, dur = 0;
        BandFrame b;
    };
    struct Second {
        double t = 0;
        float peak[2] = {0, 0};
        double ll = 0, rr = 0, lr = 0;
        long n = 0;
        double lufsM = NAN, lufsS = NAN, tp = NAN;
        double bandDur = 0;
        double mid[kBands] = {}, side[kBands] = {};  // power × seconds while open, mean power when closed
    };

    void prepare(double sr);
    void processPiece(const float* l, const float* r, int n, double t);
    void closeSecond();
    void onFrame(const BandFrame& f, double t);
    void windowStats(double windowS, float peak[2], double& ll, double& rr, double& lr, long& n) const;
    bool hasSignal() const;

    // identity
    mutable std::mutex idm_;
    Identity id_;
    std::atomic<int> probeId_{0};
    std::atomic<uint32_t> version_{1};
    std::atomic<int> channels_{2};

    // audio
    AudioRing ring_;
    std::atomic<double> lastAudioT_{-1};

    // analysis (guarded by m_)
    mutable std::mutex m_;
    std::unique_ptr<Engine> engine_;
    double sr_ = 0;
    int engineChannels_ = 0;
    int tickLen_ = 480;
    Tick cur_;
    std::vector<Tick> ticks_;  // ring
    size_t tickHead_ = 0, tickCount_ = 0;
    std::vector<Frame> frames_;
    size_t frameHead_ = 0, frameCount_ = 0;
    double lastFrameT_ = -1;
    double pieceEndT_ = 0;
    Second sec_;
    bool secOpen_ = false;
    std::vector<Second> history_;  // ring of closed seconds
    size_t histHead_ = 0, histCount_ = 0;
    long silentRun_ = 0;
    double resetT_ = 0;
    std::vector<float> bufL_, bufR_;

    // delay capture (guarded by m_)
    void capture(const float* l, const float* r, const BlockInfo& bi);
    std::vector<float> cap_;
    std::vector<CaptureBlock> capBlocks_;
    uint64_t capWritten_ = 0, capRunStart_ = 0, capDropped_ = 0;
    size_t capBlockHead_ = 0, capBlockCount_ = 0;
    double capUntil_ = -1, capRate_ = 0;
};

}  // namespace liveprobe
