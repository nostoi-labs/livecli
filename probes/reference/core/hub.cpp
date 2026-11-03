// SPDX-License-Identifier: MIT
#include "hub.h"

#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <pthread.h>

#include "server.h"

namespace liveprobe {

// ============================================================== analysis thread

AnalysisThread::AnalysisThread(double hz) : period_(1.0 / hz) {
    th_ = std::thread([this] { run(); });
}

AnalysisThread::~AnalysisThread() {
    {
        std::lock_guard<std::mutex> lk(m_);
        stop_ = true;
    }
    cv_.notify_all();
    if (th_.joinable()) th_.join();
}

void AnalysisThread::run() {
#ifdef __APPLE__
    pthread_setname_np("liveprobe.analysis");
#endif
    auto next = std::chrono::steady_clock::now();
    const auto period = std::chrono::duration_cast<std::chrono::steady_clock::duration>(std::chrono::duration<double>(period_));
    std::unique_lock<std::mutex> lk(m_);
    while (!stop_) {
        lk.unlock();
        for (auto& inst : Hub::get().all()) inst->analyze();
        lk.lock();
        next += period;
        auto now = std::chrono::steady_clock::now();
        if (next < now) next = now + period;  // fell behind: never burst
        cv_.wait_until(lk, next, [this] { return stop_; });
    }
}

// ============================================================== hub

Hub& Hub::get() {
    static Hub* hub = new Hub();  // never destroyed: plugin unload order is up to the host
    return *hub;
}

Hub::~Hub() = default;

std::shared_ptr<Instance> Hub::add() {
    std::lock_guard<std::mutex> life(lifecycle_);
    std::shared_ptr<Instance> inst;
    bool first;
    {
        std::lock_guard<std::mutex> lk(m_);
        char uid[48];
        snprintf(uid, sizeof uid, "%s-%04x-%04llu", product().uidPrefix.c_str(), (unsigned)(getpid() & 0xFFFF),
                 (unsigned long long)nextSerial_);
        inst = std::make_shared<Instance>(uid, nextSerial_++);
        first = instances_.empty();
        instances_.push_back(inst);
    }
    if (first || !analysis_) {
        if (!analysis_) analysis_.reset(new AnalysisThread(30.0));
        if (serverEnabled_ && !server_) {
            server_.reset(new Server());
            if (!server_->start()) server_.reset();
        }
    }
    return inst;
}

void Hub::remove(const std::shared_ptr<Instance>& inst) {
    std::lock_guard<std::mutex> life(lifecycle_);
    bool empty;
    {
        std::lock_guard<std::mutex> lk(m_);
        for (auto it = instances_.begin(); it != instances_.end(); ++it)
            if (*it == inst) { instances_.erase(it); break; }
        empty = instances_.empty();
    }
    if (empty) {
        // the server sends instance_removed on its next round; give it that round before closing
        if (server_) usleep(150 * 1000);
        server_.reset();
        analysis_.reset();
    }
}

std::vector<std::shared_ptr<Instance>> Hub::all() const {
    std::lock_guard<std::mutex> lk(m_);
    return instances_;
}

std::shared_ptr<Instance> Hub::findUid(const std::string& uid) const {
    std::lock_guard<std::mutex> lk(m_);
    for (auto& i : instances_) if (i->uid == uid) return i;
    return nullptr;
}

std::shared_ptr<Instance> Hub::findSerial(uint64_t serial) const {
    std::lock_guard<std::mutex> lk(m_);
    for (auto& i : instances_) if (i->serial == serial) return i;
    return nullptr;
}

std::vector<std::shared_ptr<Instance>> Hub::findProbeId(int probeId) const {
    std::lock_guard<std::mutex> lk(m_);
    std::vector<std::shared_ptr<Instance>> out;
    for (auto& i : instances_) if (i->probeId() == probeId) out.push_back(i);
    return out;
}

void Hub::setHostName(const std::string& name) {
    std::lock_guard<std::mutex> lk(m_);
    if (!name.empty()) host_ = name;
}

std::string Hub::hostName() const {
    std::lock_guard<std::mutex> lk(m_);
    return host_;
}

std::string Hub::socketPath() const {
    return server_ ? server_->path() : std::string();
}

}  // namespace liveprobe
