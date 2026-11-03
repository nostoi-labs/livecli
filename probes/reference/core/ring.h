// SPDX-License-Identifier: MIT
// Live Probe core: lock-free single-producer / single-consumer ring for stereo audio.
// The audio thread writes whole blocks (samples + a BlockInfo), the analysis thread reads them.
// When the reader falls behind, new blocks are dropped (never partially written).
#pragma once

#include <atomic>
#include <cstdint>
#include <cstring>
#include <vector>

namespace liveprobe {

/// Host transport at the first frame of a block (VST3 ProcessContext), copied by the audio thread.
struct Transport {
    enum : uint32_t { kPlaying = 1, kMusicValid = 2, kTempoValid = 4, kCycleActive = 8 };
    uint32_t flags = 0;
    double ppq = 0;        // projectTimeMusic, quarter notes
    double tempo = 0;      // bpm
    double cycleStart = 0, cycleEnd = 0;  // loop, quarter notes
};

struct BlockInfo {
    int32_t frames = 0;
    int32_t channels = 2;
    double t = 0;             // server clock (monotonic seconds) at the start of the block
    int64_t samplePos = -1;   // host sample position of the first frame, -1 when unknown
    double sampleRate = 48000;
    Transport transport;
};

class AudioRing {
public:
    static constexpr int kMaxChunk = 4096;   // frames per stored block (bigger host blocks are split)

    explicit AudioRing(int capacityFrames = 1 << 17, int capacityBlocks = 1 << 10) {
        int c = 1;
        while (c < capacityFrames) c <<= 1;
        cap_ = c;
        mask_ = c - 1;
        left_.assign(cap_, 0.f);
        right_.assign(cap_, 0.f);
        int b = 1;
        while (b < capacityBlocks) b <<= 1;
        bcap_ = b;
        bmask_ = b - 1;
        blocks_.resize(bcap_);
    }

    /// Audio thread. `r` may be null for mono (then only the left channel is stored).
    /// Returns false when the block was dropped.
    bool write(const float* l, const float* r, int frames, const BlockInfo& info) {
        bool ok = true;
        int done = 0;
        while (done < frames) {
            int n = frames - done;
            if (n > kMaxChunk) n = kMaxChunk;
            BlockInfo bi = info;
            bi.frames = n;
            bi.t = info.t + done / info.sampleRate;
            if (info.samplePos >= 0) bi.samplePos = info.samplePos + done;
            if (info.transport.flags & Transport::kPlaying)  // chunks of a big block: song position moves on
                bi.transport.ppq = info.transport.ppq + done / info.sampleRate * info.transport.tempo / 60.0;
            if (!writeChunk(l + done, r ? r + done : nullptr, n, bi)) ok = false;
            done += n;
        }
        return ok;
    }

    /// Analysis thread. Reads one block into l/r (at least kMaxChunk frames each).
    bool read(BlockInfo& info, float* l, float* r) {
        uint64_t bw = bwrite_.load(std::memory_order_acquire);
        uint64_t br = bread_.load(std::memory_order_relaxed);
        if (bw == br) return false;
        info = blocks_[br & bmask_];
        uint64_t rd = read_.load(std::memory_order_relaxed);
        copyOut(left_, rd, l, info.frames);
        copyOut(right_, rd, r, info.frames);
        read_.store(rd + info.frames, std::memory_order_release);
        bread_.store(br + 1, std::memory_order_release);
        return true;
    }

    /// Reader side: discards everything unread.
    void drain() {
        BlockInfo bi;
        static thread_local std::vector<float> scratch(kMaxChunk * 2);
        while (read(bi, scratch.data(), scratch.data() + kMaxChunk)) {}
    }

    uint64_t dropped() const { return dropped_.load(std::memory_order_relaxed); }

private:
    bool writeChunk(const float* l, const float* r, int n, const BlockInfo& bi) {
        uint64_t w = write_.load(std::memory_order_relaxed);
        uint64_t rd = read_.load(std::memory_order_acquire);
        uint64_t bw = bwrite_.load(std::memory_order_relaxed);
        uint64_t br = bread_.load(std::memory_order_acquire);
        if (cap_ - (w - rd) < (uint64_t)n || bcap_ - (bw - br) < 1) {
            dropped_.fetch_add(1, std::memory_order_relaxed);
            return false;
        }
        copyIn(left_, w, l, n);
        copyIn(right_, w, r ? r : l, n);
        blocks_[bw & bmask_] = bi;
        write_.store(w + n, std::memory_order_release);
        bwrite_.store(bw + 1, std::memory_order_release);
        return true;
    }

    void copyIn(std::vector<float>& dst, uint64_t at, const float* src, int n) {
        uint64_t p = at & mask_;
        uint64_t first = cap_ - p < (uint64_t)n ? cap_ - p : (uint64_t)n;
        std::memcpy(dst.data() + p, src, first * sizeof(float));
        if (first < (uint64_t)n) std::memcpy(dst.data(), src + first, (n - first) * sizeof(float));
    }

    void copyOut(const std::vector<float>& src, uint64_t at, float* dst, int n) const {
        uint64_t p = at & mask_;
        uint64_t first = cap_ - p < (uint64_t)n ? cap_ - p : (uint64_t)n;
        std::memcpy(dst, src.data() + p, first * sizeof(float));
        if (first < (uint64_t)n) std::memcpy(dst + first, src.data(), (n - first) * sizeof(float));
    }

    uint64_t cap_, mask_, bcap_, bmask_;
    std::vector<float> left_, right_;
    std::vector<BlockInfo> blocks_;
    alignas(64) std::atomic<uint64_t> write_{0};
    alignas(64) std::atomic<uint64_t> read_{0};
    alignas(64) std::atomic<uint64_t> bwrite_{0};
    alignas(64) std::atomic<uint64_t> bread_{0};
    std::atomic<uint64_t> dropped_{0};
};

}  // namespace liveprobe
