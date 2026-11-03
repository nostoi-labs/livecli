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
#include "onsets.h"
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
    /// `tr`: host transport at the first frame (null: unknown).
    void pushAudio(const float* l, const float* r, int frames, double sampleRate, int64_t samplePos, double t = -1,
                   const Transport* tr = nullptr);

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

    // ---- pulse (§5.11): test impulses on the grid, the only time a probe alters its output ----
    struct PulseRequest {
        double every = 1.0, offset = 0.0;  // quarter notes
        double levelDb = -20;               // capped at kPulseMaxDb
        bool click = false;                 // 1 ms raised-cosine burst instead of one sample
        bool replace = false;               // mute the input while pulsing
        double seconds = 4;                 // capped at 10
        double untilT = 0;                  // server clock
    };
    static constexpr double kPulseMaxDb = -6.0;
    /// Server/main thread: start, or stop (also from setActive(false) / terminate). Lock-free for the audio thread.
    void startPulse(const PulseRequest& r);
    void stopPulse();
    /// Audio thread, after the input was copied to the output: adds the impulses that fall in this block.
    /// Returns true while the output differs from the input. No locks, no allocation.
    bool renderPulse(float* const* out, int channels, int frames, const Transport& tr, double sampleRate, double t);
    bool pulsing() const { return pulsing_.load(std::memory_order_relaxed); }
    /// Transport flags of the last block (audio thread writes), and when it arrived.
    uint32_t lastTransportFlags() const { return lastFlags_.load(std::memory_order_relaxed); }
    double lastAudioT() const { return lastAudioT_.load(std::memory_order_relaxed); }

    // ---- onsets (only while a client asks) ----
    /// Server thread: detect onsets until `until` (server clock), at least down to `thresholdDb`.
    void armOnsets(double until, double thresholdDb);
    struct OnsetWindow {
        std::vector<Onset> onsets;    // t in [t0, t1), level >= threshold
        bool played = false;          // some audio of the window came with the transport playing
        bool covered = false;         // the analysis has reached t1
        double sampleRate = 0;
    };
    OnsetWindow onsetsBetween(double t0, double t1, double thresholdDb);

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
    std::atomic<uint32_t> lastFlags_{0};

    // pulse: command handed over with a seqlock (writers serialised by pulseWriteM_), state owned by the audio thread
    std::mutex pulseWriteM_;
    std::atomic<uint32_t> pulseSeq_{0};
    std::atomic<bool> cmdStop_{true}, cmdClick_{false}, cmdReplace_{false};
    std::atomic<double> cmdEvery_{1}, cmdOffset_{0}, cmdLevel_{0.1}, cmdSeconds_{4}, cmdUntil_{0};
    std::atomic<bool> pulsing_{false};
    uint32_t pulseSeen_ = 0;
    bool pActive_ = false, pPlayed_ = false, pClick_ = false, pReplace_ = false;
    double pEvery_ = 1, pOffset_ = 0, pLevel_ = 0.1, pUntil_ = 0;
    long long pRemaining_ = 0;
    int pBurstLeft_ = 0, pBurstLen_ = 0;

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

    // onsets (guarded by m_)
    OnsetDetector onsetDet_;
    double onsetUntil_ = -1, onsetThrDb_ = 0;
    std::vector<Onset> onsets_;
    std::vector<std::pair<double, double>> playRuns_;  // [start, end) of blocks with the transport playing
    double analyzedT_ = -1;
};

}  // namespace liveprobe
