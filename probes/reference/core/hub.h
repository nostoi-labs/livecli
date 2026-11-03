// SPDX-License-Identifier: MIT
// Live Probe core: the per-process registry of instances, the shared analysis thread and the
// socket server. The first instance starts both, the last one stops them.
#pragma once

#include <atomic>
#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

#include "instance.h"

namespace liveprobe {

class Server;

class AnalysisThread {
public:
    explicit AnalysisThread(double hz = 30.0);
    ~AnalysisThread();

private:
    void run();
    double period_;
    std::mutex m_;
    std::condition_variable cv_;
    bool stop_ = false;
    std::thread th_;
};

class Hub {
public:
    static Hub& get();

    /// Main thread (plugin initialize). Starts the analysis thread and the server on the first one.
    std::shared_ptr<Instance> add();
    /// Main thread (plugin terminate). Stops everything after the last one.
    void remove(const std::shared_ptr<Instance>& inst);

    std::vector<std::shared_ptr<Instance>> all() const;
    std::shared_ptr<Instance> findUid(const std::string& uid) const;
    std::shared_ptr<Instance> findSerial(uint64_t serial) const;
    std::vector<std::shared_ptr<Instance>> findProbeId(int probeId) const;

    void setHostName(const std::string& name);
    std::string hostName() const;
    /// Path of the running socket ("" when none).
    std::string socketPath() const;

    /// Module unload (bundleExit): stops the server and the analysis thread even if a host leaked
    /// instances, so no thread runs code of an unmapped bundle.
    void shutdown();

    /// For tests: run without the socket server (in-process protocol only).
    void setServerEnabled(bool on) { serverEnabled_ = on; }

private:
    Hub() = default;
    ~Hub();
    mutable std::mutex m_;          // instance list
    std::mutex lifecycle_;          // start/stop of the threads
    std::vector<std::shared_ptr<Instance>> instances_;
    uint64_t nextSerial_ = 1;
    std::string host_;
    std::unique_ptr<AnalysisThread> analysis_;
    std::unique_ptr<Server> server_;
    bool serverEnabled_ = true;
};

}  // namespace liveprobe
