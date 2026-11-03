// SPDX-License-Identifier: MIT
#include "server.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <pthread.h>
#include <pwd.h>
#include <signal.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <map>

#include "delay.h"
#include "thd.h"
#include "engine.h"
#include "hub.h"

namespace liveprobe {

// ============================================================== protocol

namespace {

void writeValue(JWriter& w, const JValue& v) {
    switch (v.type) {
        case JValue::Null: w.null(); break;
        case JValue::Bool: w.boolean(v.b); break;
        case JValue::Number:
            if (v.num == std::floor(v.num) && std::fabs(v.num) < 1e15) w.integer((long long)v.num);
            else w.number(v.num, 6);
            break;
        case JValue::String: w.str(v.str); break;
        case JValue::Array:
            w.beginArray();
            for (auto& x : v.arr) writeValue(w, x);
            w.endArray();
            break;
        case JValue::Object:
            w.beginObject();
            for (auto& kv : v.obj) { w.key(kv.first.c_str()); writeValue(w, kv.second); }
            w.endObject();
            break;
    }
}

struct Reply {
    JWriter w;
    explicit Reply(const JValue* rid, bool ok = true) {
        w.beginObject();
        w.key("ok").boolean(ok);
        if (rid) { w.key("rid"); writeValue(w, *rid); }
    }
    std::string done() { w.endObject(); return w.take(); }
};

std::string error(const JValue* rid, const std::string& code, const std::string& msg) {
    Reply r(rid, false);
    r.w.key("error").str(msg);
    r.w.key("code").str(code);
    return r.done();
}

double numberOr(const JValue& req, const char* key, double def, double lo, double hi) {
    const JValue* v = req.get(key);
    if (!v || !v->isNumber()) return def;
    return std::max(lo, std::min(hi, v->num));
}

bool flag(const JValue& req, const char* key) {
    const JValue* v = req.get(key);
    return v && v->type == JValue::Bool && v->b;
}

/// Resolves {"uid":...} or {"probe_id":N}. On failure sets err (a full error line).
std::shared_ptr<Instance> resolve(const JValue& req, const JValue* rid, std::string& err, const char* uidKey = "uid") {
    Hub& hub = Hub::get();
    const JValue* u = req.get(uidKey);
    if (u && u->isString()) {
        auto inst = hub.findUid(u->str);
        if (!inst) err = error(rid, "not_found", "no instance with uid " + u->str);
        return inst;
    }
    const JValue* p = (std::strcmp(uidKey, "uid") == 0) ? req.get("probe_id") : u;
    if (p && p->isNumber()) {
        auto list = hub.findProbeId((int)p->num);
        if (list.empty()) {
            err = error(rid, "not_found", "no instance with probe_id " + std::to_string((int)p->num));
            return nullptr;
        }
        if (list.size() > 1) {
            std::string uids;
            for (auto& i : list) uids += (uids.empty() ? "" : ", ") + i->uid;
            err = error(rid, "bad_request",
                        "probe_id " + std::to_string((int)p->num) + " matches " + std::to_string(list.size()) +
                            " instances (" + uids + "); use uid");
            return nullptr;
        }
        return list[0];
    }
    err = error(rid, "bad_request", std::string("missing ") + uidKey + (std::strcmp(uidKey, "uid") == 0 ? " or probe_id" : ""));
    return nullptr;
}

std::vector<std::string> stringList(const JValue* v) {
    std::vector<std::string> out;
    if (v && v->type == JValue::Array)
        for (auto& x : v->arr) if (x.isString()) out.push_back(x.str);
    return out;
}

bool hasCap(const char* c) {
    for (auto& x : product().capabilities) if (x == c) return true;
    return false;
}

std::string jsonOf(const JValue* v) {
    if (!v) return "";
    JWriter w;
    writeValue(w, *v);
    return w.take();
}

/// Tries a pending delay: true when `out` holds the final answer line.
bool runDelay(const PendingDelay& p, double now, std::string& out) {
    Hub& hub = Hub::get();
    auto a = hub.findUid(p.a), b = hub.findUid(p.b);
    auto reply = [&](bool ok) {
        JWriter w;
        w.beginObject();
        w.key("ok").boolean(ok);
        if (!p.ridJson.empty()) w.key("rid").raw(p.ridJson);
        return w;
    };
    if (!a || !b) {
        JWriter w = reply(false);
        w.key("error").str("instance removed while measuring");
        w.key("code").str("not_found");
        w.endObject();
        out = w.take();
        return true;
    }
    Instance::CaptureCopy ca, cb;
    a->copyCapture(ca);
    b->copyCapture(cb);
    DelayResult r = measureDelay(ca, cb, p.seconds, p.maxLagS);
    if (!r.ok && r.retry && now < p.deadline) return false;
    JWriter w = reply(r.ok);
    if (r.ok) {
        w.key("a").str(p.a);
        w.key("b").str(p.b);
        w.key("delay_samples").integer(r.delaySamples);
        w.key("delay_ms").number(r.delaySamples * 1000.0 / r.sampleRate, 3);
        w.key("correlation_peak").number(r.correlationPeak, 3);
        w.key("polarity").integer(r.polarity);
        w.key("ambiguity").number(r.ambiguity, 3);
        w.key("sample_rate").number(r.sampleRate, 1);
        w.key("seconds").number(r.seconds, 2);
        w.key("aligned").str(r.aligned);
    } else {
        w.key("error").str(r.error);
        w.key("code").str(r.code);
    }
    w.endObject();
    out = w.take();
    return true;
}

/// Answers a pending onsets request once its window is analysed: true when `out` holds the line.
bool runOnsets(const PendingOnsets& p, double now, std::string& out) {
    auto inst = Hub::get().findUid(p.uid);
    JWriter w;
    w.beginObject();
    auto head = [&](bool ok) {
        w.key("ok").boolean(ok);
        if (!p.ridJson.empty()) w.key("rid").raw(p.ridJson);
    };
    auto err = [&](const char* code, const std::string& msg) {
        head(false);
        w.key("error").str(msg);
        w.key("code").str(code);
        w.endObject();
        out = w.take();
        return true;
    };
    if (!inst) return err("not_found", "instance removed while collecting");
    Instance::OnsetWindow win = inst->onsetsBetween(p.t0, p.t1, p.thresholdDb);
    if (!win.covered && now < p.deadline) return false;  // the analysis has not reached the end yet
    if (!win.played) return err("busy", "transport not playing (or no song position from the host)");
    if (win.onsets.empty()) return err("busy", "no onsets above the threshold");
    head(true);
    w.key("uid").str(p.uid);
    w.key("sample_rate").number(win.sampleRate, 1);
    w.key("seconds").number(p.t1 - p.t0, 2);
    w.key("threshold_db").number(p.thresholdDb, 1);
    w.key("onsets").beginArray();
    for (const Onset& o : win.onsets) {
        w.beginObject();
        w.key("t").number(o.t, 4);
        w.key("ppq").number(o.ppq, 6);
        w.key("tempo").number(o.tempo, 3);
        w.key("level_db").number(o.levelDb, 1);
        w.endObject();
    }
    w.endArray();
    w.endObject();
    out = w.take();
    return true;
}

void writeThd(JWriter& w, const ThdResult& t) {
    auto db = [&](const char* k, double v) { w.key(k); if (std::isfinite(v) && v > -300) w.number(v, 2); else w.null(); };
    w.key("freq").number(t.freq, 3);
    db("fundamental_db", t.fundamentalDb);
    w.key("thd_pct").number(t.thdPct, 5);
    db("thd_db", t.thdDb);
    w.key("thd_n_pct").number(t.thdNPct, 5);
    db("thd_n_db", t.thdNDb);
    db("sinad_db", -t.thdNDb);
    db("noise_db", t.noiseDb);
    db("snr_db", t.snrDb);
    w.key("harmonics_db").beginArray();
    for (double h : t.harmonicsDb) { if (std::isfinite(h) && h > -300) w.number(h, 2); else w.null(); }
    w.endArray();
    w.key("clipping").boolean(t.clipping);
}

/// Answers a pending thd request once its recording is complete: true when `out` holds the line.
bool runThd(const PendingThd& p, double now, std::string& out) {
    auto inst = Hub::get().findUid(p.uid);
    JWriter w;
    w.beginObject();
    auto head = [&](bool ok) { w.key("ok").boolean(ok); if (!p.ridJson.empty()) w.key("rid").raw(p.ridJson); };
    auto err = [&](const std::string& code, const std::string& msg) {
        head(false);
        w.key("error").str(msg);
        w.key("code").str(code);
        w.endObject();
        out = w.take();
        return true;
    };
    if (!inst) return err("not_found", "instance removed while recording");
    if (!inst->takeRecording(false).done && now < p.deadline) return false;
    Instance::Recording rec = inst->takeRecording(true);
    if (rec.l.empty()) return err("busy", "no audio recorded (is the track processed?)");
    std::vector<ThdResult> ch;
    ch.push_back(measureThd(rec.l.data(), rec.l.size(), rec.sampleRate, p.freq));
    if (rec.channels > 1) ch.push_back(measureThd(rec.r.data(), rec.r.size(), rec.sampleRate, p.freq));
    // the worst channel goes on top (the chain is as good as its worst side)
    size_t worst = 0;
    for (size_t c = 0; c < ch.size(); c++) {
        if (!ch[c].ok) return err(ch[c].code, ch.size() > 1 ? std::string(c ? "right: " : "left: ") + ch[c].error : ch[c].error);
        if (ch[c].thdNPct > ch[worst].thdNPct) worst = c;
    }
    head(true);
    w.key("uid").str(p.uid);
    w.key("sample_rate").number(rec.sampleRate, 1);
    w.key("seconds").number(rec.l.size() / rec.sampleRate, 3);
    w.key("fft_size").integer(ch[worst].fftSize);
    w.key("frames").integer(ch[worst].frames);
    w.key("channel").integer((long long)worst);
    writeThd(w, ch[worst]);
    w.key("channels").beginArray();
    for (auto& c : ch) { w.beginObject(); writeThd(w, c); w.endObject(); }
    w.endArray();
    w.endObject();
    out = w.take();
    return true;
}

}  // namespace

void Protocol::duePending(Session& s, double now, std::string& out) {
    for (size_t i = 0; i < s.pendingThd.size();) {
        PendingThd& p = s.pendingThd[i];
        std::string line;
        if (now >= p.due && runThd(p, now, line)) {
            out += line;
            out += '\n';
            s.pendingThd.erase(s.pendingThd.begin() + (long)i);
            continue;
        }
        if (now >= p.due) p.due = now + 0.1;
        i++;
    }
    for (size_t i = 0; i < s.pendingOnsets.size();) {
        PendingOnsets& p = s.pendingOnsets[i];
        std::string line;
        if (now >= p.due && runOnsets(p, now, line)) {
            out += line;
            out += '\n';
            s.pendingOnsets.erase(s.pendingOnsets.begin() + (long)i);
            continue;
        }
        if (now >= p.due) p.due = now + 0.1;
        i++;
    }
    for (size_t i = 0; i < s.pending.size();) {
        PendingDelay& p = s.pending[i];
        std::string line;
        if (now >= p.due && runDelay(p, now, line)) {
            out += line;
            out += '\n';
            s.pending.erase(s.pending.begin() + (long)i);
            continue;
        }
        if (now >= p.due) p.due = now + 0.25;
        i++;
    }
}

std::string Protocol::handle(const std::string& line, Session& s, double now) {
    JValue req;
    std::string perr;
    if (!parseJson(line, req, perr)) return error(nullptr, "bad_request", "bad json: " + perr);
    if (req.type != JValue::Object) return error(nullptr, "bad_request", "request must be an object");
    const JValue* rid = req.get("rid");
    const JValue* cmdv = req.get("cmd");
    if (!cmdv || !cmdv->isString()) return error(rid, "bad_request", "missing cmd");
    const std::string& cmd = cmdv->str;
    Hub& hub = Hub::get();
    const ProductInfo& prod = product();
    std::string err;

    if (cmd == "hello") {
        Reply r(rid);
        r.w.key("protocol").str(kProtocolVersion);
        r.w.key("product").str(prod.name);
        r.w.key("product_id").str(prod.id);
        r.w.key("version").str(prod.version);
        r.w.key("pid").integer(getpid());
        r.w.key("host").str(hub.hostName());
        r.w.key("capabilities").beginArray();
        for (auto& c : prod.capabilities) r.w.str(c);
        r.w.endArray();
        r.w.key("instances").integer((long long)hub.all().size());
        return r.done();
    }
    if (cmd == "list") {
        Reply r(rid);
        r.w.key("instances").beginArray();
        for (auto& i : hub.all()) i->writeIdentity(r.w, now);
        r.w.endArray();
        return r.done();
    }
    if (cmd == "get") {
        double win = numberOr(req, "window_s", 0.3, 0.1, 10.0);
        std::vector<std::string> fields = stringList(req.get("fields"));
        const std::vector<std::string>* fp = req.get("fields") ? &fields : nullptr;
        if (flag(req, "all")) {
            Reply r(rid);
            r.w.key("snapshots").beginArray();
            for (auto& i : hub.all()) i->writeSnapshot(r.w, win, now, fp);
            r.w.endArray();
            return r.done();
        }
        auto inst = resolve(req, rid, err);
        if (!inst) return err;
        Reply r(rid);
        r.w.key("snapshot");
        inst->writeSnapshot(r.w, win, now, fp);
        return r.done();
    }
    if (cmd == "history") {
        auto inst = resolve(req, rid, err);
        if (!inst) return err;
        int sec = (int)numberOr(req, "seconds", 120, 1, 120);
        Reply r(rid);
        r.w.key("uid").str(inst->uid);
        r.w.key("summaries");
        inst->writeHistory(r.w, sec, now);
        return r.done();
    }
    if (cmd == "compare") {
        if (!hasCap("compare")) return error(rid, "unsupported", "compare is not supported");
        auto a = resolve(req, rid, err, "a");
        if (!a) return err;
        auto b = resolve(req, rid, err, "b");
        if (!b) return err;
        int sec = (int)numberOr(req, "seconds", 10, 1, 120);
        auto sa = a->secondBands(sec, now), sb = b->secondBands(sec, now);
        double pa[kBands] = {}, pb[kBands] = {};
        int used = 0;
        size_t j = 0;
        for (auto& x : sa) {
            while (j < sb.size() && sb[j].t < x.t) j++;
            if (j < sb.size() && sb[j].t == x.t) {
                for (int k = 0; k < kBands; k++) { pa[k] += x.mid[k]; pb[k] += sb[j].mid[k]; }
                used++;
            }
        }
        if (used == 0) return error(rid, "busy", "no common audio seconds in the last " + std::to_string(sec) + " s");
        Reply r(rid);
        r.w.key("a").str(a->uid);
        r.w.key("b").str(b->uid);
        r.w.key("seconds").integer(used);
        r.w.key("centers").beginArray();
        for (int k = 0; k < kBands; k++) r.w.number(bandCenter(k), 1);
        r.w.endArray();
        r.w.key("gain_db").beginArray();
        for (int k = 0; k < kBands; k++) {
            double x = pa[k] / used, y = pb[k] / used;
            if (x > 1e-15 && y > 1e-15) r.w.number(10 * std::log10(y / x), 1); else r.w.null();
        }
        r.w.endArray();
        if (flag(req, "transfer")) r.w.key("transfer").null();
        return r.done();
    }
    if (cmd == "delay") {
        if (!hasCap("delay")) return error(rid, "unsupported", "delay is not supported");
        auto a = resolve(req, rid, err, "a");
        if (!a) return err;
        auto b = resolve(req, rid, err, "b");
        if (!b) return err;
        if (a == b) return error(rid, "bad_request", "a and b are the same instance");
        PendingDelay p;
        p.ridJson = jsonOf(rid);
        p.a = a->uid;
        p.b = b->uid;
        p.seconds = numberOr(req, "seconds", 5, 0.5, 10);
        p.maxLagS = numberOr(req, "max_lag_ms", 1000, 1, 1000) / 1000.0;
        const double need = p.seconds + 2 * p.maxLagS + 0.5;
        if (need > std::min(a->captureCapacityS(), b->captureCapacityS()))
            return error(rid, "bad_request", "seconds + 2 * max_lag too long for this sample rate");
        a->armCapture(now);
        b->armCapture(now);
        p.due = now;
        p.deadline = now + need + 2.0;
        std::string line;
        if (runDelay(p, now, line)) return line;  // already captured: answer now
        p.due = now + 0.25;
        s.pending.push_back(p);
        return std::string();
    }
    if (cmd == "pulse") {
        if (!hasCap("pulse")) return error(rid, "unsupported", "pulse is not supported");
        auto inst = resolve(req, rid, err);
        if (!inst) return err;
        if (flag(req, "stop")) {
            inst->stopPulse();
            Reply r(rid);
            r.w.key("uid").str(inst->uid);
            r.w.key("stopped").boolean(true);
            return r.done();
        }
        Instance::PulseRequest p;
        p.every = numberOr(req, "every", 1.0, 1.0 / 32, 64);
        p.offset = numberOr(req, "offset", 0.0, -64, 64);
        p.seconds = numberOr(req, "seconds", 4, 0.5, 10);
        p.levelDb = std::min(numberOr(req, "level_db", -20, -120, 0), Instance::kPulseMaxDb);
        std::string shape = "impulse", mode = "add";
        if (const JValue* v = req.get("shape")) shape = v->isString() ? v->str : "?";
        if (const JValue* v = req.get("mode")) mode = v->isString() ? v->str : "?";
        if (shape != "impulse" && shape != "click") return error(rid, "bad_request", "shape: impulse or click");
        if (mode != "add" && mode != "replace") return error(rid, "bad_request", "mode: add or replace");
        p.click = shape == "click";
        p.replace = mode == "replace";
        if (now - inst->lastAudioT() > 1.0) return error(rid, "busy", "no audio processed in the last second");
        const uint32_t f = inst->lastTransportFlags();
        if (!(f & Transport::kMusicValid) || !(f & Transport::kTempoValid))
            return error(rid, "bad_request", "the host gives no song position or tempo: no pulses");
        p.untilT = now + p.seconds;
        inst->startPulse(p);
        Reply r(rid);
        r.w.key("uid").str(inst->uid);
        r.w.key("until_t").number(p.untilT, 3);
        r.w.key("level_db").number(p.levelDb, 1);
        r.w.key("every").number(p.every, 6);
        r.w.key("offset").number(p.offset, 6);
        r.w.key("seconds").number(p.seconds, 2);
        r.w.key("shape").str(shape);
        r.w.key("mode").str(mode);
        r.w.key("playing").boolean((f & Transport::kPlaying) != 0);
        return r.done();
    }
    if (cmd == "signal") {
        if (!hasCap("signal")) return error(rid, "unsupported", "signal is not supported");
        auto inst = resolve(req, rid, err);
        if (!inst) return err;
        if (flag(req, "stop")) {
            inst->stopSignal(false);  // 10 ms fade out
            Reply r(rid);
            r.w.key("uid").str(inst->uid);
            r.w.key("stopped").boolean(true);
            return r.done();
        }
        if (flag(req, "status")) {
            Instance::SignalStatus st = inst->signalStatus();
            Reply r(rid);
            r.w.key("uid").str(inst->uid);
            r.w.key("active").boolean(st.active);
            r.w.key("run").integer(st.run);
            if (st.started) {
                r.w.key("started_t").number(st.startT, 6);
                r.w.key("start_sample_pos").integer(st.startSamplePos);
                r.w.key("start_ppq");
                if (st.startPpqValid) r.w.number(st.startPpq, 6); else r.w.null();
                r.w.key("elapsed_s").number(st.elapsedS, 4);
            }
            return r.done();
        }
        Instance::SignalRequest q;
        std::string kind = "pink", mode = "replace";
        if (const JValue* v = req.get("kind")) kind = v->isString() ? v->str : "?";
        if (const JValue* v = req.get("mode")) mode = v->isString() ? v->str : "?";
        if (kind == "pink") q.kind = Instance::SignalKind::Pink;
        else if (kind == "sine") q.kind = Instance::SignalKind::Sine;
        else if (kind == "sweep") q.kind = Instance::SignalKind::Sweep;
        else if (kind == "silence") q.kind = Instance::SignalKind::Silence;
        else return error(rid, "bad_request", "kind: pink, sine, sweep or silence");
        if (mode != "replace" && mode != "add") return error(rid, "bad_request", "mode: replace or add");
        q.add = mode == "add";
        if (now - inst->lastAudioT() > 1.0) return error(rid, "busy", "no audio processed in the last second");
        const double sr = inst->identity().sampleRate;
        const double nyq = 0.45 * sr;
        q.seconds = numberOr(req, "seconds", 5, 0.5, 30);
        q.freq = numberOr(req, "freq", 1000, 1, nyq);
        q.from = numberOr(req, "from", 20, 1, nyq);
        q.to = numberOr(req, "to", 20000, 1, nyq);
        if (q.kind == Instance::SignalKind::Sweep && !(q.to > q.from * 1.01))
            return error(rid, "bad_request", "sweep: to must be above from");
        const double maxDb = q.kind == Instance::SignalKind::Pink ? Instance::kSignalMaxPinkRmsDb : Instance::kSignalMaxPeakDb;
        q.levelDb = std::min(numberOr(req, "level_db", -20, -120, 0), maxDb);
        if (const JValue* v = req.get("seed"))
            if (v->isNumber() && v->num >= 1) q.seed = (uint64_t)v->num;
        q.untilT = now + q.seconds + 0.5;  // safety net if the host stops calling the plug-in
        inst->startSignal(q);
        Reply r(rid);
        r.w.key("uid").str(inst->uid);
        r.w.key("until_t").number(q.untilT, 3);
        r.w.key("kind").str(kind);
        r.w.key("level_db").number(q.levelDb, 1);
        r.w.key("level_ref").str(q.kind == Instance::SignalKind::Pink ? "rms" : "peak");
        r.w.key("seconds").number(q.seconds, 3);
        r.w.key("fade_ms").number(Instance::kSignalFadeS * 1000.0, 1);
        r.w.key("mode").str(mode);
        r.w.key("sample_rate").number(sr, 1);
        if (q.kind == Instance::SignalKind::Sine) r.w.key("freq").number(q.freq, 3);
        if (q.kind == Instance::SignalKind::Sweep) {
            r.w.key("from").number(q.from, 3);
            r.w.key("to").number(q.to, 3);
            r.w.key("sweep").str("exponential: f(t) = from * exp(t / seconds * ln(to / from)), phase 0 at the first sample");
        }
        if (q.seed) r.w.key("seed").integer((long long)q.seed);
        return r.done();
    }
    if (cmd == "thd") {
        if (!hasCap("thd")) return error(rid, "unsupported", "thd is not supported");
        auto inst = resolve(req, rid, err);
        if (!inst) return err;
        if (now - inst->lastAudioT() > 1.0) return error(rid, "busy", "no audio processed in the last second");
        const double sr = inst->identity().sampleRate;
        PendingThd p;
        p.ridJson = jsonOf(rid);
        p.uid = inst->uid;
        p.freq = numberOr(req, "freq", 0, 0, 1e6);
        if (p.freq > 0 && (p.freq < 20 || p.freq > 0.45 * sr))
            return error(rid, "bad_request", "freq must be 20 Hz … 0.45 × sample rate");
        // at least one FFT frame (65536 samples at 48 kHz = 1.37 s) plus margin; at most 10 s
        const double minS = thdFftSize(sr) / sr + 0.05;
        const double sec = std::max(minS, numberOr(req, "seconds", 2, 0.5, 10));
        p.t1 = now + sec;
        p.due = p.t1 + 0.1;
        p.deadline = p.t1 + 1.5;
        if (!inst->armRecord(now, p.t1, sr)) return error(rid, "busy", "a thd measurement is already running on this probe");
        s.pendingThd.push_back(p);
        return std::string();
    }
    if (cmd == "onsets") {
        if (!hasCap("onsets")) return error(rid, "unsupported", "onsets is not supported");
        auto inst = resolve(req, rid, err);
        if (!inst) return err;
        PendingOnsets p;
        p.ridJson = jsonOf(rid);
        p.uid = inst->uid;
        const double sec = numberOr(req, "seconds", 8, 0.5, 30);
        p.thresholdDb = numberOr(req, "threshold_db", -50, -100, 0);
        p.t0 = now;
        p.t1 = now + sec;
        p.due = p.t1 + 0.1;
        p.deadline = p.t1 + 1.5;  // the analysis runs ~30 Hz behind the audio
        inst->armOnsets(p.t1, p.thresholdDb);
        s.pendingOnsets.push_back(p);
        return std::string();
    }
    if (cmd == "subscribe") {
        Subscription sub;
        sub.hz = numberOr(req, "hz", 10, 1, 30);
        sub.windowS = numberOr(req, "window_s", 0.3, 0.1, 10.0);
        if (req.get("fields")) { sub.hasFields = true; sub.fields = stringList(req.get("fields")); }
        sub.next = now;
        bool any = false;
        if (flag(req, "all")) { sub.all = true; any = true; }
        else if (req.get("uid") || req.get("probe_id")) {
            auto inst = resolve(req, rid, err);
            if (!inst) return err;
            sub.uid = inst->uid;
            any = true;
        }
        s.events = true;
        if (any) {
            s.subs.erase(std::remove_if(s.subs.begin(), s.subs.end(),
                                        [&](const Subscription& x) { return x.all == sub.all && x.uid == sub.uid; }),
                         s.subs.end());
            s.subs.push_back(sub);
        }
        Reply r(rid);
        r.w.key("subscriptions").integer((long long)s.subs.size());
        return r.done();
    }
    if (cmd == "unsubscribe") {
        if (req.get("uid") || req.get("probe_id")) {
            auto inst = resolve(req, rid, err);
            if (!inst) return err;
            s.subs.erase(std::remove_if(s.subs.begin(), s.subs.end(),
                                        [&](const Subscription& x) { return !x.all && x.uid == inst->uid; }),
                         s.subs.end());
        } else {
            s.subs.clear();
            s.events = false;
        }
        Reply r(rid);
        r.w.key("subscriptions").integer((long long)s.subs.size());
        return r.done();
    }
    if (cmd == "reset") {
        std::vector<std::shared_ptr<Instance>> list;
        if (flag(req, "all")) list = hub.all();
        else {
            auto inst = resolve(req, rid, err);
            if (!inst) return err;
            list.push_back(inst);
        }
        for (auto& i : list) i->reset();
        Reply r(rid);
        r.w.key("reset").beginArray();
        for (auto& i : list) r.w.str(i->uid);
        r.w.endArray();
        return r.done();
    }
    if (cmd == "set_label") {
        auto inst = resolve(req, rid, err);
        if (!inst) return err;
        const JValue* l = req.get("label");
        if (!l || !l->isString()) return error(rid, "bad_request", "missing label");
        inst->setLabel(l->str);
        Reply r(rid);
        r.w.key("uid").str(inst->uid);
        return r.done();
    }
    return error(rid, "unsupported", "unknown cmd " + cmd);
}

void Protocol::dueEvents(Session& s, double now, std::string& out) {
    if (s.subs.empty()) return;
    Hub& hub = Hub::get();
    for (auto& sub : s.subs) {
        if (now < sub.next) continue;
        sub.next += 1.0 / sub.hz;
        if (sub.next < now) sub.next = now + 1.0 / sub.hz;  // fell behind: do not burst
        std::vector<std::shared_ptr<Instance>> list;
        if (sub.all) list = hub.all();
        else if (auto i = hub.findUid(sub.uid)) list.push_back(i);
        for (auto& inst : list) {
            JWriter w;
            w.beginObject();
            w.key("event").str("snapshot");
            w.key("uid").str(inst->uid);
            w.key("snapshot");
            inst->writeSnapshot(w, sub.windowS, now, sub.hasFields ? &sub.fields : nullptr);
            w.endObject();
            out += w.text();
            out += '\n';
        }
    }
}

int Protocol::msUntilDue(const Session& s, double now, int cap) {
    int best = cap;
    for (auto& p : s.pending) {
        int ms = (int)std::ceil((p.due - now) * 1000.0);
        if (ms < best) best = ms < 0 ? 0 : ms;
    }
    for (auto& p : s.pendingThd) {
        int ms = (int)std::ceil((p.due - now) * 1000.0);
        if (ms < best) best = ms < 0 ? 0 : ms;
    }
    for (auto& p : s.pendingOnsets) {
        int ms = (int)std::ceil((p.due - now) * 1000.0);
        if (ms < best) best = ms < 0 ? 0 : ms;
    }
    for (auto& sub : s.subs) {
        int ms = (int)std::ceil((sub.next - now) * 1000.0);
        if (ms < best) best = ms < 0 ? 0 : ms;
    }
    return best;
}

// ============================================================== server

std::string socketDir() {
    if (const char* d = getenv("LIVEPROBES_DIR")) if (*d) return d;
    const char* home = getenv("HOME");
    std::string h;
    if (home && *home) h = home;
    else if (passwd* pw = getpwuid(getuid())) h = pw->pw_dir;
    return h + "/Library/Application Support/LiveProbes";
}

std::string fallbackSocketDir() { return "/tmp/LiveProbes-" + std::to_string((unsigned long)getuid()); }

namespace {

void setNonBlock(int fd) {
    int fl = fcntl(fd, F_GETFL, 0);
    fcntl(fd, F_SETFL, fl | O_NONBLOCK);
    fcntl(fd, F_SETFD, FD_CLOEXEC);
}

/// Creates `dir` (and missing parents) with mode 0700 for the new components. The final directory
/// must be a real directory owned by us; in /tmp it must also be 0700 (it is shared with other users).
bool ensureDir(const std::string& dir, bool strict) {
    for (size_t pos = 1; pos <= dir.size(); pos++) {
        if (pos != dir.size() && dir[pos] != '/') continue;
        std::string part = dir.substr(0, pos);
        struct stat pst;
        if (stat(part.c_str(), &pst) == 0) continue;
        if (mkdir(part.c_str(), 0700) != 0 && errno != EEXIST) {
            fprintf(stderr, "[liveprobe] cannot create %s: %s\n", part.c_str(), strerror(errno));
            return false;
        }
    }
    struct stat st;
    if (lstat(dir.c_str(), &st) != 0 || !S_ISDIR(st.st_mode) || st.st_uid != getuid()) {
        fprintf(stderr, "[liveprobe] %s is not a directory owned by this user\n", dir.c_str());
        return false;
    }
    if (strict && (st.st_mode & 077) != 0 && chmod(dir.c_str(), 0700) != 0) return false;
    return true;
}

void cleanupStale(const std::string& dir, const std::string& prod) {
    DIR* d = opendir(dir.c_str());
    if (!d) return;
    const std::string prefix = prod + "-";
    while (dirent* e = readdir(d)) {
        std::string n = e->d_name;
        if (n.size() <= prefix.size() + 5 || n.compare(0, prefix.size(), prefix) != 0) continue;
        if (n.compare(n.size() - 5, 5, ".sock") != 0) continue;
        std::string mid = n.substr(prefix.size(), n.size() - prefix.size() - 5);
        if (mid.empty() || mid.size() > 9 || mid.find_first_not_of("0123456789") != std::string::npos) continue;
        pid_t pid = (pid_t)atol(mid.c_str());
        bool dead = pid == getpid() || (kill(pid, 0) == -1 && errno == ESRCH);
        if (dead) unlink((dir + "/" + n).c_str());
    }
    closedir(d);
}

struct Client {
    int fd = -1;
    std::string in, out;
    Session session;
    bool closing = false;
};

/// Best effort, bounded: used for the last events before the server closes.
void flushBlocking(Client& c, double untilT) {
    while (!c.out.empty() && monoNow() < untilT) {
        ssize_t n = send(c.fd, c.out.data(), c.out.size(), 0);
        if (n > 0) { c.out.erase(0, (size_t)n); continue; }
        if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) {
            pollfd p{c.fd, POLLOUT, 0};
            poll(&p, 1, 10);
            continue;
        }
        break;
    }
}

}  // namespace

Server::Server() = default;
Server::~Server() { stop(); }

bool Server::start() {
    const std::string prod = product().id;
    const std::string name = "/" + prod + "-" + std::to_string(getpid()) + ".sock";
    sockaddr_un addr{};
    std::string dir = socketDir();
    // §1: macOS limits sun_path to 104 bytes; long paths (long user names, long LIVEPROBES_DIR) use /tmp
    if (dir.size() + name.size() >= sizeof(addr.sun_path) || !ensureDir(dir, false)) {
        dir = fallbackSocketDir();
        if (!ensureDir(dir, true)) return false;
    }
    // stale sockets of this product, in both places a client looks
    cleanupStale(socketDir(), prod);
    cleanupStale(fallbackSocketDir(), prod);
    path_ = dir + name;
    if (path_.size() >= sizeof(addr.sun_path)) {
        fprintf(stderr, "[liveprobe] socket path too long: %s\n", path_.c_str());
        path_.clear();
        return false;
    }
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path_.c_str(), sizeof(addr.sun_path) - 1);
    listenFd_ = socket(AF_UNIX, SOCK_STREAM, 0);
    if (listenFd_ < 0) { path_.clear(); return false; }
    fcntl(listenFd_, F_SETFD, FD_CLOEXEC);
    unlink(path_.c_str());
    if (bind(listenFd_, (sockaddr*)&addr, sizeof addr) != 0 || listen(listenFd_, 16) != 0) {
        fprintf(stderr, "[liveprobe] bind %s: %s\n", path_.c_str(), strerror(errno));
        close(listenFd_);
        listenFd_ = -1;
        path_.clear();
        return false;
    }
    chmod(path_.c_str(), 0600);
    setNonBlock(listenFd_);
    if (pipe(wakePipe_) != 0) { stop(); return false; }
    setNonBlock(wakePipe_[0]);
    setNonBlock(wakePipe_[1]);
    stop_ = false;
    th_ = std::thread([this] { run(); });
    return true;
}

void Server::wake() {
    if (wakePipe_[1] >= 0) {
        char c = 1;
        (void)!write(wakePipe_[1], &c, 1);
    }
}

void Server::stop() {
    stop_ = true;
    wake();
    if (th_.joinable()) th_.join();
    if (listenFd_ >= 0) { close(listenFd_); listenFd_ = -1; }
    for (int& fd : wakePipe_) if (fd >= 0) { close(fd); fd = -1; }
    if (!path_.empty()) { unlink(path_.c_str()); path_.clear(); }
}

void Server::run() {
#ifdef __APPLE__
    pthread_setname_np("liveprobe.server");
#endif
    std::vector<Client> clients;
    struct Known { uint32_t version; std::string json; };
    std::map<uint64_t, Known> known;
    Hub& hub = Hub::get();
    {
        double now = monoNow();
        for (auto& i : hub.all()) {
            JWriter w;
            i->writeIdentity(w, now);
            known[i->serial] = {i->version(), w.take()};
        }
    }
    std::vector<pollfd> pfds;
    char buf[16384];
    while (!stop_) {
        double now = monoNow();
        int timeout = 100;
        for (auto& c : clients) timeout = Protocol::msUntilDue(c.session, now, timeout);
        pfds.clear();
        pfds.push_back({listenFd_, POLLIN, 0});
        pfds.push_back({wakePipe_[0], POLLIN, 0});
        for (auto& c : clients) pfds.push_back({c.fd, (short)(POLLIN | (c.out.empty() ? 0 : POLLOUT)), 0});
        int pr = poll(pfds.data(), (nfds_t)pfds.size(), timeout);
        if (stop_) break;
        if (pr < 0 && errno != EINTR) break;
        now = monoNow();
        if (pfds[1].revents & POLLIN) while (read(wakePipe_[0], buf, sizeof buf) > 0) {}
        if (pfds[0].revents & POLLIN) {
            while (true) {
                int fd = accept(listenFd_, nullptr, nullptr);
                if (fd < 0) break;
                if (clients.size() >= 64) { close(fd); continue; }
                setNonBlock(fd);
#ifdef SO_NOSIGPIPE
                int one = 1;
                setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &one, sizeof one);
#endif
                Client c;
                c.fd = fd;
                clients.push_back(std::move(c));
                pfds.push_back({fd, 0, 0});  // keep indices aligned; polled next round
            }
        }
        for (size_t k = 0; k < clients.size(); k++) {
            Client& c = clients[k];
            short re = (k + 2 < pfds.size()) ? pfds[k + 2].revents : 0;
            if (re & POLLIN) {
                while (true) {
                    ssize_t n = recv(c.fd, buf, sizeof buf, 0);
                    if (n > 0) { c.in.append(buf, (size_t)n); continue; }
                    if (n == 0) c.closing = true;
                    else if (errno != EAGAIN && errno != EWOULDBLOCK && errno != EINTR) c.closing = true;
                    break;
                }
                size_t pos;
                while ((pos = c.in.find('\n')) != std::string::npos) {
                    std::string line = c.in.substr(0, pos);
                    c.in.erase(0, pos + 1);
                    if (!line.empty() && line.back() == '\r') line.pop_back();
                    if (line.find_first_not_of(" \t") == std::string::npos) continue;
                    std::string resp = Protocol::handle(line, c.session, monoNow());
                    if (!resp.empty()) {
                        c.out += resp;
                        c.out += '\n';
                    }
                }
                if (c.in.size() > 1 << 20) { c.closing = true; }
            }
            if (re & (POLLHUP | POLLERR | POLLNVAL)) c.closing = true;
        }

        // instance added / removed / changed
        std::string events;
        {
            auto list = hub.all();
            std::map<uint64_t, bool> seen;
            for (auto& i : list) {
                seen[i->serial] = true;
                auto it = known.find(i->serial);
                uint32_t v = i->version();
                if (it == known.end() || it->second.version != v) {
                    JWriter w;
                    i->writeIdentity(w, now);
                    std::string js = w.take();
                    events += std::string("{\"event\":\"") + (it == known.end() ? "instance_added" : "instance_changed") +
                              "\",\"instance\":" + js + "}\n";
                    known[i->serial] = {v, js};
                }
            }
            for (auto it = known.begin(); it != known.end();) {
                if (!seen.count(it->first)) {
                    events += "{\"event\":\"instance_removed\",\"instance\":" + it->second.json + "}\n";
                    it = known.erase(it);
                } else {
                    ++it;
                }
            }
        }
        for (auto& c : clients) {
            if (c.closing) continue;
            if (!events.empty() && c.session.events) c.out += events;
            Protocol::dueEvents(c.session, now, c.out);
            Protocol::duePending(c.session, now, c.out);
            while (!c.out.empty()) {
                ssize_t n = send(c.fd, c.out.data(), c.out.size(), 0);
                if (n > 0) { c.out.erase(0, (size_t)n); continue; }
                if (n < 0 && (errno == EAGAIN || errno == EWOULDBLOCK || errno == EINTR)) break;
                c.closing = true;
                break;
            }
            if (c.out.size() > (8u << 20)) c.closing = true;  // a client that does not read
        }
        for (size_t k = 0; k < clients.size();) {
            if (clients[k].closing) {
                close(clients[k].fd);
                clients.erase(clients.begin() + (long)k);
            } else {
                k++;
            }
        }
    }
    // closing: tell subscribers about the instances that went away, then hang up
    std::string bye;
    for (auto& kv : known)
        if (!hub.findSerial(kv.first)) bye += "{\"event\":\"instance_removed\",\"instance\":" + kv.second.json + "}\n";
    const double until = monoNow() + 0.2;
    for (auto& c : clients) {
        if (!c.closing && c.session.events && !bye.empty()) {
            c.out += bye;
            flushBlocking(c, until);
        }
        close(c.fd);
    }
}

}  // namespace liveprobe
