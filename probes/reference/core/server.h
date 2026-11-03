// SPDX-License-Identifier: MIT
// Live Probe core: protocol v0.1 commands (§5) and the Unix socket server (§1-2).
#pragma once

#include <atomic>
#include <string>
#include <thread>
#include <vector>

#include "json.h"

namespace liveprobe {

constexpr const char* kProtocolVersion = "0.1";

struct Subscription {
    bool all = false;
    std::string uid;          // when !all
    double hz = 10;
    double windowS = 0.3;
    bool hasFields = false;
    std::vector<std::string> fields;
    double next = 0;
};

/// A `delay` request waiting for enough captured audio.
struct PendingDelay {
    std::string ridJson;  // "" = no rid
    std::string a, b;
    double seconds = 5, maxLagS = 1, due = 0, deadline = 0;
};

/// An `onsets` request collecting until `t1`.
struct PendingOnsets {
    std::string ridJson;
    std::string uid;
    double t0 = 0, t1 = 0, thresholdDb = -50, deadline = 0, due = 0;
};

/// Per-connection state.
struct Session {
    bool events = false;  // receives instance_added/removed/changed
    std::vector<Subscription> subs;
    std::vector<PendingDelay> pending;
    std::vector<PendingOnsets> pendingOnsets;
};

class Protocol {
public:
    /// One request line in, one response line out (no trailing newline). "" = answered later
    /// (a `delay` waiting for audio: see duePending).
    static std::string handle(const std::string& line, Session& s, double now);
    /// Appends due snapshot event lines ("...\n") for the session's subscriptions.
    static void dueEvents(Session& s, double now, std::string& out);
    /// Appends the answers of `delay` requests that are ready ("...\n").
    static void duePending(Session& s, double now, std::string& out);
    /// Milliseconds until the session's next due subscription (or `cap`).
    static int msUntilDue(const Session& s, double now, int cap);
};

/// Socket directory: $LIVEPROBES_DIR, else ~/Library/Application Support/LiveProbes.
std::string socketDir();
/// §1 fallback when the socket path would exceed 104 bytes: /tmp/LiveProbes-<uid>.
std::string fallbackSocketDir();

class Server {
public:
    Server();
    ~Server();
    /// Creates the directory, removes stale sockets of this product, binds and starts the thread.
    bool start();
    void stop();
    const std::string& path() const { return path_; }

private:
    void run();
    void wake();
    std::string path_;
    int listenFd_ = -1;
    int wakePipe_[2] = {-1, -1};
    std::atomic<bool> stop_{false};
    std::thread th_;
};

}  // namespace liveprobe
