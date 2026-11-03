// SPDX-License-Identifier: MIT
// Unit tests for the probe core and the reference DSP (no VST3, no sockets).
#include <cmath>
#include <cstdio>
#include <cstring>
#include <random>
#include <string>
#include <vector>

#include "hub.h"
#include "instance.h"
#include "json.h"
#include "reference_engine.h"
#include "ring.h"
#include "server.h"
#include "thd.h"

using namespace liveprobe;

static int failures = 0, checks = 0;
#define CHECK(cond)                                                              \
    do {                                                                         \
        checks++;                                                                \
        if (!(cond)) { failures++; fprintf(stderr, "FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } \
    } while (0)
#define NEAR(a, b, tol)                                                                                    \
    do {                                                                                                   \
        checks++;                                                                                          \
        double _a = (a), _b = (b);                                                                         \
        if (!(std::fabs(_a - _b) <= (tol))) {                                                              \
            failures++;                                                                                    \
            fprintf(stderr, "FAIL %s:%d: %s = %.3f, expected %.3f ± %.3f\n", __FILE__, __LINE__, #a, _a, _b, (double)(tol)); \
        }                                                                                                  \
    } while (0)

static JValue parse(const std::string& s) {
    JValue v;
    std::string err;
    if (!parseJson(s, v, err)) { fprintf(stderr, "bad json (%s): %s\n", err.c_str(), s.c_str()); failures++; }
    return v;
}

static JValue call(Session& s, const std::string& req, double now) { return parse(Protocol::handle(req, s, now)); }

static double num(const JValue* v) { return v && v->isNumber() ? v->num : NAN; }

/// Feeds a stereo signal in 512-frame blocks on a synthetic clock, analysing as it goes.
static double feed(Instance& in, double t, double seconds, double sr, double hz, double dbL, double dbR, bool anti = false) {
    const int block = 512;
    std::vector<float> l(block), r(block);
    double al = dbL <= -200 ? 0 : std::pow(10, dbL / 20), ar = dbR <= -200 ? 0 : std::pow(10, dbR / 20);
    long total = (long)(seconds * sr);
    static double phase = 0;
    for (long done = 0; done < total; done += block) {
        for (int i = 0; i < block; i++) {
            double x = std::sin(phase);
            phase += 2 * M_PI * hz / sr;
            l[i] = (float)(al * x);
            r[i] = (float)((anti ? -ar : ar) * x);
        }
        in.pushAudio(l.data(), r.data(), block, sr, done, t + done / sr);
        if ((done / block) % 8 == 0) in.analyze();
    }
    in.analyze();
    return t + total / sr;
}

static void testJson() {
    JValue v = parse(R"({"cmd":"get","uid":"rp-1","window_s":1.5,"all":true,"fields":["a","b"],"x":null,"s":"è\n"})");
    CHECK(v.type == JValue::Object);
    CHECK(v.get("cmd")->str == "get");
    NEAR(num(v.get("window_s")), 1.5, 1e-9);
    CHECK(v.get("all")->b);
    CHECK(v.get("fields")->arr.size() == 2);
    CHECK(v.get("s")->str == "\xc3\xa8\n");
    JValue bad;
    std::string err;
    CHECK(!parseJson("{\"a\":", bad, err));
    CHECK(!parseJson("{} x", bad, err));
    JWriter w;
    w.beginObject().key("a").db(-INFINITY).key("b").number(-0.04, 1).key("c").beginArray().integer(1).str("q\"").endArray().endObject();
    CHECK(w.text() == R"({"a":-150.0,"b":0.0,"c":[1,"q\""]})");
}

static void testJsonText() {
    // track names with quotes, backslashes, control characters, accents, emoji, broken UTF-8
    JWriter w;
    w.str("Bass \"sub\" \\ 1\t\x01 Città 🎛 \xff\xc3");
    const std::string out = w.text();
    CHECK(out == "\"Bass \\\"sub\\\" \\\\ 1\\t\\u0001 Citt\xc3\xa0 \xf0\x9f\x8e\x9b \xef\xbf\xbd\xef\xbf\xbd\"");
    JValue v;
    std::string err;
    CHECK(parseJson(out, v, err) && v.isString());
    // surrogates: a valid pair, a lone high surrogate, a lone low one
    CHECK(parseJson(R"("\ud83c\udf9b|\ud83c|\udf9b")", v, err) && v.str == "\xf0\x9f\x8e\x9b|\xef\xbf\xbd|\xef\xbf\xbd");
}

static void testSecondAlignment() {
    // pre and post on one track see the same samples, timestamped a few µs apart and in blocks that
    // straddle second boundaries: their 1-second summaries must hold the same energy
    Hub& hub = Hub::get();
    auto a = hub.add(), b = hub.add();
    a->setFormat(2, 48000);
    b->setFormat(2, 48000);
    const double sr = 48000, t0 = std::floor(monoNow()) - 40 + 0.9;
    const int block = 512;
    std::vector<float> l(block), r(block);
    double ph = 0;
    for (long done = 0; done < (long)(6 * sr); done += block) {
        double amp = ((done / block) % 7) * 0.1;  // level changes every block: misaligned seconds would differ
        for (int i = 0; i < block; i++) { l[i] = r[i] = (float)(amp * std::sin(ph)); ph += 2 * M_PI * 440 / sr; }
        a->pushAudio(l.data(), r.data(), block, sr, done, t0 + done / sr);
        b->pushAudio(l.data(), r.data(), block, sr, done, t0 + done / sr + 40e-6);
        a->analyze();
        b->analyze();
    }
    Session s;
    JValue ha = call(s, R"({"cmd":"history","uid":")" + a->uid + R"("})", t0 + 7);
    JValue hb = call(s, R"({"cmd":"history","uid":")" + b->uid + R"("})", t0 + 7);
    const JValue *sa = ha.get("summaries"), *sb = hb.get("summaries");
    CHECK(sa && sb && sa->arr.size() == sb->arr.size() && sa->arr.size() >= 5);
    if (sa && sb && sa->arr.size() == sb->arr.size()) {
        double worst = 0;
        for (size_t i = 0; i < sa->arr.size(); i++) {
            CHECK(num(sa->arr[i].get("t")) == num(sb->arr[i].get("t")));
            worst = std::max(worst, std::fabs(num(&sa->arr[i].get("rms_db")->arr[0]) - num(&sb->arr[i].get("rms_db")->arr[0])));
        }
        printf("  pre/post 40 us apart: worst 1-s RMS difference %.3f dB over %zu seconds\n", worst, sa->arr.size());
        CHECK(worst <= 0.05);
    }
    hub.remove(a);
    hub.remove(b);
}

// ---- delay: pre/post with a known delay, measured through the protocol
struct DelayCase {
    long delay;      // B late by this many samples (negative: A late)
    float bGain = 1; // -1: inverted
    bool sine = false, silentB = false;
    double srB = 48000;
};

static JValue runDelayCase(const DelayCase& dc, double tBase) {
    Hub& hub = Hub::get();
    auto a = hub.add(), b = hub.add();
    a->setFormat(2, 48000);
    b->setFormat(2, dc.srB);
    Session s;
    const double sr = 48000;
    const std::string req = R"({"cmd":"delay","rid":9,"a":")" + a->uid + R"(","b":")" + b->uid + R"(","seconds":3,"max_lag_ms":200})";
    const std::string firstLine = Protocol::handle(req, s, tBase);  // arms the capture, answered later
    JValue first;
    if (!firstLine.empty()) first = parse(firstLine);
    CHECK(firstLine.empty() && s.pending.size() == 1);
    const int block = 256;
    const long D = std::labs(dc.delay);
    std::vector<float> src(16 * 48000 + D), l(block), r(block), l2(block), r2(block);
    std::mt19937 rng(77);
    std::normal_distribution<float> g(0.f, 0.1f);
    for (size_t i = 0; i < src.size(); i++) src[i] = dc.sine ? 0.3f * (float)std::sin(2 * M_PI * 1000 * i / sr) : g(rng);
    for (long done = 0; done + block <= 8 * 48000; done += block) {
        for (int i = 0; i < block; i++) {
            long n = done + i + D;
            float xa = src[(size_t)(dc.delay >= 0 ? n : n - D)], xb = src[(size_t)(dc.delay >= 0 ? n - D : n)];
            l[i] = r[i] = xa;
            l2[i] = r2[i] = dc.silentB ? 0.f : dc.bGain * xb;
        }
        double t = tBase + done / sr;
        a->pushAudio(l.data(), r.data(), block, sr, done, t);
        b->pushAudio(l2.data(), r2.data(), block, dc.srB, done, t + 40e-6);  // post runs a little later
        if ((done / block) % 16 == 0) { a->analyze(); b->analyze(); }
    }
    a->analyze();
    b->analyze();
    std::string out;
    Protocol::duePending(s, tBase + 8.0, out);
    if (out.empty()) Protocol::duePending(s, tBase + 30.0, out);  // past the deadline: final answer
    JValue v = parse(out.empty() ? "{}" : out.substr(0, out.find('\n')));
    hub.remove(a);
    hub.remove(b);
    return first.get("ok") ? first : v;
}

static void testDelay() {
    const double t0 = std::floor(monoNow()) - 300;
    JValue v = runDelayCase({480}, t0);
    printf("  delay 480 samples: got %.0f, peak %.3f, polarity %.0f, ambiguity %.3f, aligned %s\n", num(v.get("delay_samples")),
           num(v.get("correlation_peak")), num(v.get("polarity")), num(v.get("ambiguity")),
           v.get("aligned") ? v.get("aligned")->str.c_str() : "?");
    CHECK(v.get("ok") && v.get("ok")->b && num(v.get("rid")) == 9);
    NEAR(num(v.get("delay_samples")), 480, 0);
    NEAR(num(v.get("delay_ms")), 10.0, 0.001);
    CHECK(num(v.get("correlation_peak")) > 0.99 && num(v.get("polarity")) == 1 && num(v.get("ambiguity")) < 0.2);
    CHECK(v.get("aligned") && v.get("aligned")->str == "block");

    v = runDelayCase({100, -1.f}, t0 + 100);
    printf("  delay 100 inverted: got %.0f, polarity %.0f\n", num(v.get("delay_samples")), num(v.get("polarity")));
    NEAR(num(v.get("delay_samples")), 100, 0);
    CHECK(num(v.get("polarity")) == -1);

    v = runDelayCase({-240}, t0 + 200);
    printf("  B early by 240: got %.0f\n", num(v.get("delay_samples")));
    NEAR(num(v.get("delay_samples")), -240, 0);

    v = runDelayCase({0}, t0 + 300);
    NEAR(num(v.get("delay_samples")), 0, 0);

    DelayCase sil{480};
    sil.silentB = true;
    v = runDelayCase(sil, t0 + 400);
    printf("  silent b: %s\n", v.get("error") ? v.get("error")->str.c_str() : "?");
    CHECK(v.get("ok") && !v.get("ok")->b && v.get("code")->str == "busy");

    DelayCase sine{48};
    sine.sine = true;
    v = runDelayCase(sine, t0 + 500);
    printf("  1 kHz sine: delay %.0f ambiguity %.3f\n", num(v.get("delay_samples")), num(v.get("ambiguity")));
    CHECK(num(v.get("ambiguity")) > 0.9);

    DelayCase rates{0};
    rates.srB = 44100;
    v = runDelayCase(rates, t0 + 600);
    CHECK(v.get("ok") && !v.get("ok")->b && v.get("code")->str == "bad_request");
}

// ---- onsets: clicks on the 1/16 grid of the host's song position, a known time late
struct OnsetCase {
    double lateMs = 10;      // clicks this late versus the grid (negative: early)
    double tempo = 120, tempo2 = 0;  // tempo2: tempo after the first half
    bool stopped = false, noMusic = false, loop = false;
    double clickDb = -6, thresholdDb = -50;
    double seconds = 4;
    int block = 512;
};

/// Offsets from the 1/16 grid in ms, the way `live sync` computes them.
static std::vector<double> gridOffsetsMs(const JValue& r) {
    std::vector<double> out;
    const JValue* list = r.get("onsets");
    if (!list) return out;
    for (auto& o : list->arr) {
        double ppq = num(o.get("ppq")), tempo = num(o.get("tempo"));
        double off = ppq - std::round(ppq / 0.25) * 0.25;
        out.push_back(off * 60.0 / tempo * 1000.0);
    }
    return out;
}

static JValue runOnsetCase(const OnsetCase& oc, double tBase) {
    Hub& hub = Hub::get();
    auto a = hub.add();
    a->setFormat(2, 48000);
    Session s;
    const double sr = 48000;
    char req[256];
    snprintf(req, sizeof req, R"({"cmd":"onsets","rid":"o","uid":"%s","seconds":%g,"threshold_db":%g})", a->uid.c_str(),
             oc.seconds, oc.thresholdDb);
    CHECK(Protocol::handle(req, s, tBase).empty() && s.pendingOnsets.size() == 1);
    const double amp = std::pow(10.0, oc.clickDb / 20.0);
    std::vector<float> l(oc.block), r(oc.block);
    double ppq = 1.0;              // song position (unwrapped), starts on beat 2
    long nextClick = -1;           // sample index of the next click
    double clickStart = -1;
    const long total = (long)((oc.seconds + 0.6) * sr);
    for (long done = 0; done < total; done += oc.block) {
        const double tempo = (oc.tempo2 > 0 && done > total / 2) ? oc.tempo2 : oc.tempo;
        Transport tr;
        tr.tempo = tempo;
        tr.flags = Transport::kTempoValid | (oc.stopped ? 0 : Transport::kPlaying) | (oc.noMusic ? 0 : Transport::kMusicValid);
        tr.ppq = oc.loop ? std::fmod(ppq, 2.0) : ppq;
        if (oc.loop) { tr.flags |= Transport::kCycleActive; tr.cycleStart = 0; tr.cycleEnd = 2; }
        for (int i = 0; i < oc.block; i++) {
            const long n = done + i;
            const double p0 = ppq + i / sr * tempo / 60.0, p1 = p0 + tempo / 60.0 / sr;
            // a 1/16 boundary inside this sample → click `lateMs` later
            if (std::floor(p1 / 0.25) > std::floor(p0 / 0.25) || (n == 0 && std::fmod(p0, 0.25) == 0)) {
                const double frac = (std::floor(p1 / 0.25) * 0.25 - p0) / (p1 - p0);  // boundary position in the sample
                // early clicks belong to the next boundary: schedule them one 1/16 ahead
                const double ahead = oc.lateMs < 0 ? 0.25 * 60.0 / tempo * sr : 0.0;
                nextClick = n + (long)std::lround(frac + ahead + oc.lateMs * sr / 1000.0);
            }
            if (n == nextClick) clickStart = (double)n;
            float x = 0;
            if (clickStart >= 0) {
                const double dt = (n - clickStart) / sr;
                if (dt < 0.03) x = (float)(amp * std::cos(2 * M_PI * 2000 * dt) * std::exp(-dt / 0.005));
            }
            l[i] = r[i] = x;
        }
        ppq += oc.block / sr * tempo / 60.0;
        a->pushAudio(l.data(), r.data(), oc.block, sr, done, tBase + 0.01 + done / sr, &tr);
        if ((done / oc.block) % 16 == 0) a->analyze();
    }
    a->analyze();
    std::string out;
    Protocol::duePending(s, tBase + oc.seconds + 0.2, out);
    JValue v = parse(out.empty() ? "{}" : out.substr(0, out.find('\n')));
    hub.remove(a);
    return v;
}

static void summarize(const char* what, const JValue& v, double expectMs, size_t expectN) {
    std::vector<double> off = gridOffsetsMs(v);
    double lo = 1e9, hi = -1e9, sum = 0;
    for (double x : off) { lo = std::min(lo, x); hi = std::max(hi, x); sum += x; }
    printf("  %s: %zu onsets, offset %.3f ms (min %.3f, max %.3f), tempo %.0f\n", what, off.size(),
           off.empty() ? NAN : sum / off.size(), lo, hi, v.get("onsets") && !v.get("onsets")->arr.empty() ? num(v.get("onsets")->arr[0].get("tempo")) : NAN);
    CHECK(v.get("ok") && v.get("ok")->b && v.get("rid") && v.get("rid")->str == "o");
    CHECK(off.size() + 1 >= expectN && off.size() <= expectN + 1);
    for (double x : off) NEAR(x, expectMs, 0.05);
}

static void testOnsets() {
    const double t0 = std::floor(monoNow()) - 1000;
    OnsetCase c;
    summarize("120 bpm, 10 ms late", runOnsetCase(c, t0), 10.0, 32);
    c = OnsetCase();
    c.lateMs = -3;
    summarize("120 bpm, 3 ms early", runOnsetCase(c, t0 + 10), -3.0, 32);
    c = OnsetCase();
    c.tempo = 100;
    c.lateMs = 5;
    c.block = 128;
    summarize("100 bpm, 5 ms late, 128-frame blocks", runOnsetCase(c, t0 + 20), 5.0, 27);
    c = OnsetCase();
    c.tempo2 = 90;
    summarize("120 → 90 bpm, 10 ms late", runOnsetCase(c, t0 + 30), 10.0, 28);
    c = OnsetCase();
    c.loop = true;
    JValue lv = runOnsetCase(c, t0 + 40);
    summarize("loop over 2 quarters (wraps inside blocks)", lv, 10.0, 32);
    bool inLoop = true;
    if (lv.get("onsets")) for (auto& o : lv.get("onsets")->arr) inLoop = inLoop && num(o.get("ppq")) >= 0 && num(o.get("ppq")) < 2.0;
    CHECK(inLoop);
    c = OnsetCase();
    c.clickDb = -40;
    summarize("clicks at -40 dBFS", runOnsetCase(c, t0 + 50), 10.0, 32);

    c = OnsetCase();
    c.stopped = true;
    JValue v = runOnsetCase(c, t0 + 60);
    printf("  stopped: %s\n", v.get("error") ? v.get("error")->str.c_str() : "?");
    CHECK(v.get("code") && v.get("code")->str == "busy");
    c = OnsetCase();
    c.noMusic = true;
    v = runOnsetCase(c, t0 + 70);
    CHECK(v.get("code") && v.get("code")->str == "busy");
    c = OnsetCase();
    c.clickDb = -60;
    v = runOnsetCase(c, t0 + 80);
    printf("  clicks at -60 dBFS, threshold -50: %s\n", v.get("error") ? v.get("error")->str.c_str() : "?");
    CHECK(v.get("code") && v.get("code")->str == "busy");
    c.thresholdDb = -70;
    summarize("clicks at -60 dBFS, threshold -70", runOnsetCase(c, t0 + 90), 10.0, 32);
}

// ---- pulse: impulses at exact sample indices, then bit-exact pass-through again
struct PulseRun {
    std::vector<long> at;        // sample indices where output != input
    std::vector<float> value;    // output - input there
    long lastDiff = -1;
    long bitExactAfter = 0;      // samples after the end that equal the input
};

/// Emulates the audio thread. `tempoAt(n)`/ppq follow the blocks; `transportAt(block)` can stop it.
static PulseRun runPulse(Instance& in, int block, long total, double ppq0, double tempo, double tempo2At, double tempo2,
                         bool loop, double t0, long stopAtBlock = -1, bool noise = false) {
    PulseRun pr;
    const double sr = 48000;
    std::vector<float> inL(block), inR(block), outL(block), outR(block);
    std::mt19937 rng(5);
    std::normal_distribution<float> g(0.f, 0.05f);
    double ppq = ppq0;
    for (long done = 0, b = 0; done < total; done += block, b++) {
        const double tp = (tempo2At >= 0 && done >= tempo2At) ? tempo2 : tempo;
        Transport tr;
        tr.flags = Transport::kTempoValid | Transport::kMusicValid | ((stopAtBlock >= 0 && b >= stopAtBlock) ? 0 : Transport::kPlaying);
        tr.tempo = tp;
        tr.ppq = loop ? std::fmod(ppq, 2.0) : ppq;
        if (loop) { tr.flags |= Transport::kCycleActive; tr.cycleStart = 0; tr.cycleEnd = 2; }
        for (int i = 0; i < block; i++) { inL[i] = noise ? g(rng) : 0.f; inR[i] = noise ? g(rng) : 0.f; }
        outL = inL;
        outR = inR;
        float* outs[2] = {outL.data(), outR.data()};
        in.renderPulse(outs, 2, block, tr, sr, t0 + done / sr);
        for (int i = 0; i < block; i++) {
            if (std::memcmp(&outL[i], &inL[i], 4) != 0 || std::memcmp(&outR[i], &inR[i], 4) != 0) {
                pr.at.push_back(done + i);
                pr.value.push_back(outL[i] - inL[i]);
                pr.lastDiff = done + i;
            }
        }
        if (tr.flags & Transport::kPlaying) ppq += block / sr * tp / 60.0;
    }
    pr.bitExactAfter = total - 1 - pr.lastDiff;
    return pr;
}

static void testPulse() {
    Hub& hub = Hub::get();
    auto a = hub.add();
    const double t0 = 5000;
    Instance::PulseRequest rq;
    rq.untilT = t0 + 100;

    // 120 bpm: a quarter is 24000 samples. From ppq 0.3 the quarters land at 16800, 40800, 64800 (block 512)
    rq.seconds = 2;
    a->startPulse(rq);
    PulseRun p = runPulse(*a, 512, 4 * 48000, 0.3, 120, -1, 0, false, t0);
    printf("  pulse quarters from ppq 0.3, 2 s: at");
    for (long x : p.at) printf(" %ld", x);
    printf(", level %.4f (-20 dBFS = 0.1000), bit-exact for the last %ld samples\n", p.value.empty() ? 0.f : p.value[0], p.bitExactAfter);
    CHECK((p.at == std::vector<long>{16800, 40800, 64800, 88800}));  // 2 s = 96000 samples: four quarters
    NEAR(p.value.empty() ? 0 : p.value[0], 0.1, 1e-6);
    CHECK(p.lastDiff < 96000);

    // positions on block boundaries (block 480: 50 blocks per quarter), eighths
    rq.every = 0.5;
    rq.seconds = 1;
    a->startPulse(rq);
    p = runPulse(*a, 480, 2 * 48000, 0.0, 120, -1, 0, false, t0);
    CHECK((p.at == std::vector<long>{0, 12000, 24000, 36000}));

    // a position inside a block that straddles it: block 333, offset 0.125 (= 3000 samples)
    rq.every = 1.0;
    rq.offset = 0.125;
    a->startPulse(rq);
    p = runPulse(*a, 333, 2 * 48000, 0.0, 120, -1, 0, false, t0);
    CHECK((p.at == std::vector<long>{3000, 27000}));
    rq.offset = 0;

    // tempo change at sample 48000 (120 → 90): quarters at 0, 24000, then 32000 samples apart
    rq.seconds = 3;
    a->startPulse(rq);
    p = runPulse(*a, 500, 3 * 48000, 0.0, 120, 48000, 90, false, t0);
    printf("  tempo 120 → 90 at 48000:");
    for (long x : p.at) printf(" %ld", x);
    printf("\n");
    CHECK((p.at == std::vector<long>{0, 24000, 48000, 80000, 112000}));

    // loop over 2 quarters wrapping inside a block: every 0.5 → 0, 12000, 24000, 36000, then 48000 (= ppq 0 again)
    rq.every = 0.5;
    rq.seconds = 1.5;
    a->startPulse(rq);
    p = runPulse(*a, 700, 2 * 48000, 0.0, 120, -1, 0, true, t0);
    printf("  loop 0-2 (wrap at 48000, inside a 700-frame block):");
    for (long x : p.at) printf(" %ld", x);
    printf("\n");
    CHECK((p.at == std::vector<long>{0, 12000, 24000, 36000, 48000, 60000}));
    rq.every = 1.0;

    // level cap: 0 dB asked → -6 dBFS
    rq.levelDb = 0;
    rq.seconds = 1;
    a->startPulse(rq);
    p = runPulse(*a, 512, 48000, 0.0, 120, -1, 0, false, t0);
    NEAR(p.value.empty() ? 0 : p.value[0], 0.501187, 1e-5);
    rq.levelDb = -20;

    // added to the input, and bit-exact again after the duration (noise input)
    rq.seconds = 1;
    a->startPulse(rq);
    p = runPulse(*a, 512, 3 * 48000, 0.0, 120, -1, 0, false, t0, -1, true);
    CHECK((p.at == std::vector<long>{0, 24000}));
    NEAR(p.value.size() > 1 ? p.value[1] : 0, 0.1, 1e-6);
    CHECK(p.bitExactAfter > 48000);

    // replace: input muted while pulsing (every sample of the active second differs)
    rq.replace = true;
    a->startPulse(rq);
    p = runPulse(*a, 512, 2 * 48000, 0.0, 120, -1, 0, false, t0, -1, true);
    CHECK(p.at.size() > 40000 && p.lastDiff < 48000 + 512);
    rq.replace = false;

    // click shape: 1 ms raised cosine (48 samples) starting on the grid
    rq.click = true;
    rq.seconds = 0.6;
    a->startPulse(rq);
    p = runPulse(*a, 512, 48000, 0.0, 120, -1, 0, false, t0);
    CHECK(p.at.size() == 96 && p.at.front() == 0 && p.at[47] == 47 && p.at[48] == 24000);  // two clicks in 0.6 s
    rq.click = false;

    // stops when the transport stops, and does not come back
    rq.seconds = 10;
    a->startPulse(rq);
    p = runPulse(*a, 512, 4 * 48000, 0.0, 120, -1, 0, false, t0, /*stop at block*/ 100);
    CHECK((p.at == std::vector<long>{0, 24000, 48000}));  // stops at block 100 = sample 51200
    p = runPulse(*a, 512, 2 * 48000, 0.0, 120, -1, 0, false, t0);
    CHECK(p.at.empty());

    // stop command; wall-clock deadline
    a->startPulse(rq);
    a->stopPulse();
    p = runPulse(*a, 512, 48000, 0.0, 120, -1, 0, false, t0);
    CHECK(p.at.empty());
    rq.untilT = t0 + 0.6;
    a->startPulse(rq);
    p = runPulse(*a, 512, 2 * 48000, 0.0, 120, -1, 0, false, t0);
    printf("  wall-clock deadline 0.6 s:");
    for (long x : p.at) printf(" %ld", x);
    printf("\n");
    CHECK((p.at == std::vector<long>{0, 24000}));

    // protocol: no song position → bad_request; stop → ok
    Session s;
    Transport none;
    std::vector<float> z(256, 0.f);
    a->pushAudio(z.data(), z.data(), 256, 48000, 0, monoNow(), &none);
    JValue v = call(s, R"({"cmd":"pulse","uid":")" + a->uid + R"("})", monoNow());
    CHECK(v.get("code") && v.get("code")->str == "bad_request");
    Transport play;
    play.flags = Transport::kPlaying | Transport::kMusicValid | Transport::kTempoValid;
    play.tempo = 120;
    a->pushAudio(z.data(), z.data(), 256, 48000, 0, monoNow(), &play);
    v = call(s, R"({"cmd":"pulse","uid":")" + a->uid + R"(","level_db":3,"seconds":60})", monoNow());
    printf("  pulse request: level_db %.1f, seconds %.1f\n", num(v.get("level_db")), num(v.get("seconds")));
    CHECK(v.get("ok")->b && num(v.get("level_db")) == -6.0 && num(v.get("seconds")) == 10.0);
    v = call(s, R"({"cmd":"pulse","uid":")" + a->uid + R"(","mode":"loud"})", monoNow());
    CHECK(v.get("code")->str == "bad_request");
    v = call(s, R"({"cmd":"pulse","uid":")" + a->uid + R"(","stop":true})", monoNow());
    CHECK(v.get("ok")->b && v.get("stopped")->b);
    hub.remove(a);
}

// ---- signal: generators through the audio-thread path
struct SignalRun {
    std::vector<float> outL, inL;
    long firstDiff = -1, lastDiff = -1;
};

static SignalRun runSignal(Instance& in, long total, bool noiseIn, int block = 512, double t0 = 7000, long stopAt = -1,
                           bool stopNow = false, double tStep = 1.0) {
    SignalRun sr;
    std::mt19937 rng(9);
    std::normal_distribution<float> g(0.f, 0.05f);
    std::vector<float> L(block), R(block), oL(block), oR(block);
    Transport none;  // the transport does not matter for `signal`
    for (long done = 0; done < total; done += block) {
        if (stopAt >= 0 && done >= stopAt && done < stopAt + block) in.stopSignal(stopNow);
        for (int i = 0; i < block; i++) { L[i] = noiseIn ? g(rng) : 0.f; R[i] = L[i]; }
        oL = L;
        oR = R;
        float* outs[2] = {oL.data(), oR.data()};
        in.renderSignal(outs, 2, block, none, done, 48000, t0 + tStep * done / 48000.0);
        for (int i = 0; i < block; i++) {
            sr.outL.push_back(oL[i]);
            sr.inL.push_back(L[i]);
            if (std::memcmp(&oL[i], &L[i], 4) != 0) {
                if (sr.firstDiff < 0) sr.firstDiff = done + i;
                sr.lastDiff = done + i;
            }
        }
    }
    return sr;
}

static double rmsDb(const std::vector<float>& x, long a, long b) {
    double e = 0;
    for (long i = a; i < b; i++) e += (double)x[(size_t)i] * x[(size_t)i];
    return 10 * std::log10(e / (double)(b - a));
}
static double peakDb(const std::vector<float>& x, long a, long b) {
    float p = 0;
    for (long i = a; i < b; i++) p = std::max(p, std::fabs(x[(size_t)i]));
    return 20 * std::log10(p);
}

static void testSignal() {
    Hub& hub = Hub::get();
    auto a = hub.add();
    Instance::SignalRequest rq;
    rq.untilT = 1e12;

    // sine 1 kHz at -20 dBFS peak, 1 s, replacing silence: peak -20.0, RMS -23.0, 10 ms fades, then bit-exact
    rq.kind = Instance::SignalKind::Sine;
    rq.seconds = 1;
    a->startSignal(rq);
    SignalRun r = runSignal(*a, 96000, false);
    printf("  sine -20 dBFS: peak %.2f, RMS %.2f, first sample %.5f, at 5 ms %.4f, ends at sample %ld\n", peakDb(r.outL, 4800, 43200),
           rmsDb(r.outL, 4800, 43200), r.outL[0], std::fabs(r.outL[240]), r.lastDiff);
    NEAR(peakDb(r.outL, 4800, 43200), -20.0, 0.01);
    NEAR(rmsDb(r.outL, 4800, 43200), -23.01, 0.01);
    CHECK(std::fabs(r.outL[1]) < 0.001 && peakDb(r.outL, 0, 120) < -30);  // fade in
    CHECK(peakDb(r.outL, 48000 - 60, 48000) < -30);                        // fade out
    CHECK(r.lastDiff < 48000);  // 1 s exactly, then the input again (bit-exact)

    // level cap: 0 dB asked → -6 dBFS peak
    rq.levelDb = 0;
    a->startSignal(rq);
    r = runSignal(*a, 48000, false);
    NEAR(peakDb(r.outL, 0, 48000), -6.0, 0.01);
    rq.levelDb = -20;

    // add mode: the input stays, the sine on top; replace (default) mutes the input after the fade
    rq.add = true;
    a->startSignal(rq);
    r = runSignal(*a, 96000, true);
    double diffRms = 0;
    for (long i = 4800; i < 43200; i++) { double d = r.outL[(size_t)i] - r.inL[(size_t)i]; diffRms += d * d; }
    NEAR(10 * std::log10(diffRms / 38400), -23.01, 0.01);
    CHECK(r.lastDiff < 48000);
    rq.add = false;
    a->startSignal(rq);
    r = runSignal(*a, 96000, true);
    NEAR(rmsDb(r.outL, 4800, 43200), -23.01, 0.01);  // the noise input is gone
    bool backExact = true;
    for (long i = 48000; i < 96000; i++) backExact = backExact && r.outL[(size_t)i] == r.inL[(size_t)i];
    CHECK(backExact);

    // sweep 20 Hz → 20 kHz over 5 s: instantaneous frequency from zero crossings vs 20 · 1000^(t/5)
    rq.kind = Instance::SignalKind::Sweep;
    rq.seconds = 5;
    a->startSignal(rq);
    r = runSignal(*a, 5 * 48000 + 4800, false);
    printf("  sweep 20-20000 Hz, 5 s:");
    for (double tt : {1.0, 2.5, 4.0}) {
        long c = (long)(tt * 48000);
        std::vector<double> zc;
        for (long i = c - 2400; i < c + 2400; i++)
            if (r.outL[(size_t)i] <= 0 && r.outL[(size_t)i + 1] > 0) zc.push_back(i + r.outL[(size_t)i] / (r.outL[(size_t)i] - r.outL[(size_t)i + 1]));
        // frequency at the middle: crossings closest to the centre
        double f = NAN;
        for (size_t k = 1; k < zc.size(); k++)
            if (zc[k - 1] <= c && zc[k] > c) f = 48000.0 / (zc[k] - zc[k - 1]);
        const double expect = 20.0 * std::pow(1000.0, tt / 5.0);
        printf(" t=%.1f s %.1f Hz (expected %.1f)", tt, f, expect);
        NEAR(f / expect, 1.0, 0.01);
    }
    printf(", peak %.2f dBFS\n", peakDb(r.outL, 4800, 5 * 48000 - 4800));
    NEAR(peakDb(r.outL, 4800, 5 * 48000 - 4800), -20.0, 0.02);

    // pink noise at -20 dBFS RMS, seed 42, 10 s: RMS, repeatable, flat in 1/3-octave bands (through the band engine)
    rq.kind = Instance::SignalKind::Pink;
    rq.seconds = 10;
    rq.seed = 42;
    a->startSignal(rq);
    r = runSignal(*a, 10 * 48000, false);
    a->startSignal(rq);
    SignalRun r2 = runSignal(*a, 10 * 48000, false);
    CHECK(r.outL == r2.outL);
    printf("  pink -20 dBFS RMS: RMS %.2f, peak %.2f dBFS\n", rmsDb(r.outL, 4800, 10 * 48000 - 4800), peakDb(r.outL, 0, 10 * 48000));
    NEAR(rmsDb(r.outL, 4800, 10 * 48000 - 4800), -20.0, 0.3);
    CHECK(peakDb(r.outL, 0, 10 * 48000) <= -6.0 + 1e-3);
    {
        auto b = hub.add();
        b->setFormat(2, 48000);
        const double tb = std::floor(monoNow()) - 2000;
        for (long done = 0; done < 10 * 48000; done += 480) {
            b->pushAudio(&r.outL[(size_t)done], &r.outL[(size_t)done], 480, 48000, done, tb + done / 48000.0);
            if ((done / 480) % 16 == 0) b->analyze();
        }
        b->analyze();
        Session s;
        JValue v = call(s, R"({"cmd":"get","uid":")" + b->uid + R"(","window_s":10})", tb + 10);
        const JValue* mid = v.get("snapshot")->get("bands")->get("mid");
        double lo = 1e9, hi = -1e9, sum = 0;
        int cnt = 0;
        for (int k = 2; k <= 29; k++) {  // 31.7 Hz … 16 kHz
            double x = num(&mid->arr[(size_t)k]);
            lo = std::min(lo, x); hi = std::max(hi, x); sum += x; cnt++;
        }
        printf("  pink in 1/3-octave bands 31.7 Hz-16 kHz: mean %.2f dB, min %.2f, max %.2f (spread %.2f dB); 20 Hz %.2f, 20 kHz %.2f\n",
               sum / cnt, lo, hi, hi - lo, num(&mid->arr[0]), num(&mid->arr[30]));
        CHECK(hi - lo < 2.0);
        hub.remove(b);
    }
    rq.seed = 0;

    // pink asked at -6 dBFS RMS → capped at -18 dBFS RMS
    rq.levelDb = -6;
    rq.seconds = 3;
    a->startSignal(rq);
    r = runSignal(*a, 3 * 48000, false);
    NEAR(rmsDb(r.outL, 4800, 3 * 48000 - 4800), -18.0, 0.4);
    CHECK(peakDb(r.outL, 0, 3 * 48000) <= -6.0 + 1e-3);
    rq.levelDb = -20;

    // stop with fade (10 ms) and immediate stop; wall-clock deadline; silence replaces the input
    rq.kind = Instance::SignalKind::Sine;
    rq.seconds = 10;
    a->startSignal(rq);
    r = runSignal(*a, 2 * 48000, false, 512, 7000, 24000);
    printf("  stop at 24000 with fade: last changed sample %ld\n", r.lastDiff);
    CHECK(r.lastDiff >= 24000 && r.lastDiff < 24576 + 480);
    a->startSignal(rq);
    r = runSignal(*a, 2 * 48000, false, 512, 7000, 24064, true);
    CHECK(r.lastDiff < 24064);
    rq.untilT = 7000 + 0.5;
    a->startSignal(rq);
    r = runSignal(*a, 2 * 48000, false);
    CHECK(r.lastDiff < 24576);
    rq.untilT = 1e12;
    rq.kind = Instance::SignalKind::Silence;
    rq.seconds = 1;
    a->startSignal(rq);
    r = runSignal(*a, 96000, true);
    CHECK(rmsDb(r.outL, 480, 47520) < -300 || peakDb(r.outL, 480, 47520) < -200);
    CHECK(r.lastDiff < 48000);

    // protocol: bad kind, bad sweep, status with start position
    Session s;
    std::vector<float> z(256, 0.f);
    Transport play;
    play.flags = Transport::kMusicValid | Transport::kTempoValid;
    play.ppq = 8.0;
    a->setFormat(2, 48000);
    a->pushAudio(z.data(), z.data(), 256, 48000, 0, monoNow(), &play);
    JValue v = call(s, R"({"cmd":"signal","uid":")" + a->uid + R"(","kind":"square"})", monoNow());
    CHECK(v.get("code") && v.get("code")->str == "bad_request");
    v = call(s, R"({"cmd":"signal","uid":")" + a->uid + R"(","kind":"sweep","from":1000,"to":100})", monoNow());
    CHECK(v.get("code") && v.get("code")->str == "bad_request");
    v = call(s, R"({"cmd":"signal","uid":")" + a->uid + R"(","kind":"pink","level_db":0,"seconds":99})", monoNow());
    printf("  signal request: level_db %.1f (%s), seconds %.1f, mode %s\n", num(v.get("level_db")), v.get("level_ref")->str.c_str(),
           num(v.get("seconds")), v.get("mode")->str.c_str());
    CHECK(v.get("ok")->b && num(v.get("level_db")) == -18.0 && num(v.get("seconds")) == 30.0 && v.get("mode")->str == "replace");
    std::vector<float> o1(512, 0.f), o2(512, 0.f);
    float* outs[2] = {o1.data(), o2.data()};
    a->renderSignal(outs, 2, 512, play, 123456, 48000, monoNow());
    v = call(s, R"({"cmd":"signal","uid":")" + a->uid + R"(","status":true})", monoNow());
    printf("  status: active %d, start_sample_pos %.0f, start_ppq %.3f\n", (int)v.get("active")->b, num(v.get("start_sample_pos")),
           num(v.get("start_ppq")));
    CHECK(v.get("active")->b && num(v.get("start_sample_pos")) == 123456 && num(v.get("start_ppq")) == 8.0);
    v = call(s, R"({"cmd":"signal","uid":")" + a->uid + R"(","stop":true})", monoNow());
    CHECK(v.get("ok")->b && v.get("stopped")->b);
    hub.remove(a);
}

// ---- thd: sine with known harmonics and noise
static std::vector<float> toneWith(double sr, double f, double peakDb, double h2Dbc, double h3Dbc, double noiseRmsDb, double seconds,
                                   unsigned seed, double phase0 = 0.3) {
    const size_t n = (size_t)(seconds * sr);
    std::vector<float> x(n);
    const double a = std::pow(10, peakDb / 20), a2 = a * std::pow(10, h2Dbc / 20), a3 = a * std::pow(10, h3Dbc / 20);
    const double nr = noiseRmsDb > -300 ? std::pow(10, noiseRmsDb / 20) : 0;
    std::mt19937 rng(seed);
    std::normal_distribution<double> g(0, 1);
    for (size_t i = 0; i < n; i++) {
        const double t = i / sr;
        double v = a * std::sin(2 * M_PI * f * t + phase0);
        if (h2Dbc > -300) v += a2 * std::sin(2 * M_PI * 2 * f * t + 1.1);
        if (h3Dbc > -300) v += a3 * std::sin(2 * M_PI * 3 * f * t + 2.2);
        if (nr > 0) v += nr * g(rng);
        x[i] = (float)v;
    }
    return x;
}

static void testThd() {
    // a clean float sine: the numerical floor
    std::vector<float> x = toneWith(48000, 1000, -20, -400, -400, -400, 2, 1);
    ThdResult r = measureThd(x.data(), x.size(), 48000, 1000);
    printf("  pure 1 kHz -20 dBFS: f %.4f Hz, fundamental %.3f dBFS, THD %.1f dB, THD+N %.1f dB, noise %.1f dBFS\n", r.freq,
           r.fundamentalDb, r.thdDb, r.thdNDb, r.noiseDb);
    CHECK(r.ok && r.thdDb < -120 && r.thdNDb < -120);
    NEAR(r.fundamentalDb, -20.0, 0.01);
    NEAR(r.freq, 1000.0, 0.01);

    // H2 -40 dBc + H3 -60 dBc: THD = sqrt(1e-4 + 1e-6) = 1.00499 % = -39.957 dB, at several frequencies and rates
    struct C { double sr, f; } cases[] = {{48000, 1000}, {48000, 997.3}, {44100, 1000}, {44100, 61.7}, {96000, 1000}, {96000, 5123.4},
                                          {48000, 6000}};
    for (auto& c : cases) {
        x = toneWith(c.sr, c.f, -12, -40, -60, -400, 2, 2);
        r = measureThd(x.data(), x.size(), c.sr, 0);  // no hint: detect the fundamental
        printf("  %5.0f Hz @ %3.0f k: f %.3f, THD %.4f %% (%.3f dB), H2 %.2f, H3 %.2f, H4 %.1f dBc, THD+N %.4f %%\n", c.f, c.sr / 1000,
               r.freq, r.thdPct, r.thdDb, r.harmonicsDb[0], r.harmonicsDb[1], r.harmonicsDb[2], r.thdNPct);
        CHECK(r.ok);
        NEAR(r.freq, c.f, 0.02);
        NEAR(r.thdPct, 1.00499, 0.002);
        NEAR(r.thdDb, -39.957, 0.02);
        NEAR(r.harmonicsDb[0], -40.0, 0.02);
        NEAR(r.harmonicsDb[1], -60.0, 0.05);
        NEAR(r.fundamentalDb, -12.0, 0.01);
    }
    // harmonics above 20 kHz are left out: 6 kHz has H2, H3 and nothing from H4 on
    CHECK(std::isnan(r.harmonicsDb[2]));

    // known noise: -20 dBFS sine + white noise -80 dBFS RMS → THD+N = noise in 20 Hz…20 kHz outside the notch
    x = toneWith(48000, 1000, -20, -400, -400, -80, 3, 3);
    r = measureThd(x.data(), x.size(), 48000, 1000);
    const double bw = (20000.0 - 20.0) - (1000 * M_SQRT2 - 1000 / M_SQRT2);  // Hz of band outside the notch
    const double expectN = 10 * std::log10(1e-8 * bw / 24000.0 / (0.01 / 2));
    printf("  1 kHz -20 dBFS + noise -80 dBFS RMS: THD+N %.2f dB (expected %.2f), noise %.2f dBFS, SNR %.1f dB\n", r.thdNDb, expectN,
           r.noiseDb, r.snrDb);
    NEAR(r.thdNDb, expectN, 0.3);

    // repeatability: 10 runs, random phase and noise, H2 at -50 dBc
    double lo = 1e9, hi = -1e9;
    for (unsigned k = 0; k < 10; k++) {
        x = toneWith(48000, 1000, -20, -50, -400, -100, 2, 100 + k, 0.7 * k);
        r = measureThd(x.data(), x.size(), 48000, 1000);
        lo = std::min(lo, r.thdPct);
        hi = std::max(hi, r.thdPct);
    }
    printf("  repeatability, 10 runs (H2 -50 dBc = 0.3162 %%): THD %.5f … %.5f %% (spread %.3f %% of the value)\n", lo, hi, 100 * (hi - lo) / lo);
    CHECK((hi - lo) / lo < 0.01);

    // refusals
    std::vector<float> z(96000, 0.f);
    r = measureThd(z.data(), z.size(), 48000, 1000);
    CHECK(!r.ok && r.code == "busy");
    x = toneWith(48000, 1000, -400, -400, -400, -20, 2, 4);  // noise only
    r = measureThd(x.data(), x.size(), 48000, 1000);
    printf("  noise only: %s\n", r.error.c_str());
    CHECK(!r.ok && r.code == "busy");
    x = toneWith(48000, 1000, -20, -400, -400, -400, 0.5, 5);
    r = measureThd(x.data(), x.size(), 48000, 1000);
    CHECK(!r.ok && r.code == "busy");  // shorter than one frame
    x = toneWith(48000, 1000, -20, -400, -400, -400, 2, 6);
    r = measureThd(x.data(), x.size(), 48000, 5);
    CHECK(!r.ok && r.code == "bad_request");
    x = toneWith(48000, 1000, 0.5, -400, -400, -400, 2, 7);  // over full scale
    r = measureThd(x.data(), x.size(), 48000, 1000);
    CHECK(r.clipping);

    // protocol path: request, recording by the analysis thread, deferred answer
    Hub& hub = Hub::get();
    auto a = hub.add();
    a->setFormat(2, 48000);
    Session s;
    const double t0 = monoNow();
    std::vector<float> l = toneWith(48000, 440, -20, -46, -400, -400, 3, 8), rr = toneWith(48000, 440, -20, -60, -400, -400, 3, 9);
    a->pushAudio(l.data(), rr.data(), 64, 48000, 0, t0 - 0.01);  // audio seen in the last second
    a->analyze();
    std::string first = Protocol::handle(R"({"cmd":"thd","rid":5,"uid":")" + a->uid + R"(","freq":440,"seconds":2})", s, t0);
    CHECK(first.empty() && s.pendingThd.size() == 1);
    for (size_t done = 0; done + 480 <= l.size(); done += 480) {
        a->pushAudio(l.data() + done, rr.data() + done, 480, 48000, (int64_t)done, t0 + 0.001 + done / 48000.0);
        if ((done / 480) % 16 == 0) a->analyze();
    }
    a->analyze();
    std::string out;
    Protocol::duePending(s, t0 + 2.2, out);
    JValue v = parse(out.empty() ? "{}" : out.substr(0, out.find('\n')));
    printf("  thd command: ok %d, freq %.2f, THD %.4f %% (worst = channel %.0f), left H2 %.2f dBc, right H2 %.2f dBc\n",
           v.get("ok") && v.get("ok")->b, num(v.get("freq")), num(v.get("thd_pct")), num(v.get("channel")),
           v.get("channels") ? num(&v.get("channels")->arr[0].get("harmonics_db")->arr[0]) : NAN,
           v.get("channels") ? num(&v.get("channels")->arr[1].get("harmonics_db")->arr[0]) : NAN);
    CHECK(v.get("ok") && v.get("ok")->b && num(v.get("rid")) == 5 && num(v.get("channel")) == 0);
    NEAR(num(v.get("thd_pct")), 0.5012, 0.002);  // H2 -46 dBc
    NEAR(num(v.get("fundamental_db")), -20.0, 0.02);
    v = call(s, R"({"cmd":"thd","uid":")" + a->uid + R"(","freq":5})", t0 + 2.3);
    CHECK(v.get("code") && v.get("code")->str == "bad_request");
    hub.remove(a);
}

static void testRing() {
    AudioRing ring(1024, 16);
    std::vector<float> a(300), b(300), l(AudioRing::kMaxChunk), r(AudioRing::kMaxChunk);
    for (int round = 0; round < 20; round++) {
        for (int i = 0; i < 300; i++) { a[i] = round * 1000 + i; b[i] = -a[i]; }
        BlockInfo bi;
        bi.t = round;
        bi.sampleRate = 48000;
        CHECK(ring.write(a.data(), b.data(), 300, bi));
        BlockInfo out;
        CHECK(ring.read(out, l.data(), r.data()));
        CHECK(out.frames == 300 && out.t == round);
        CHECK(std::memcmp(l.data(), a.data(), 300 * 4) == 0 && std::memcmp(r.data(), b.data(), 300 * 4) == 0);
    }
    BlockInfo bi;
    bi.sampleRate = 48000;
    CHECK(ring.write(a.data(), b.data(), 300, bi));
    CHECK(ring.write(a.data(), b.data(), 300, bi));
    CHECK(ring.write(a.data(), b.data(), 300, bi));
    CHECK(!ring.write(a.data(), b.data(), 300, bi));  // 1200 > 1024: dropped, not torn
    CHECK(ring.dropped() == 1);
    // big host blocks are split into chunks
    AudioRing big(1 << 15, 16);
    std::vector<float> x(10000, 0.25f);
    CHECK(big.write(x.data(), nullptr, 10000, bi));
    int n = 0, frames = 0;
    BlockInfo o;
    while (big.read(o, l.data(), r.data())) { n++; frames += o.frames; CHECK(r[0] == 0.25f); }
    CHECK(n == 3 && frames == 10000);
}

static void testSnapshot() {
    Hub& hub = Hub::get();
    auto a = hub.add();
    a->setFormat(2, 48000);
    double t0 = std::floor(monoNow()) - 20 + 0.25;  // starts a quarter into a second
    // 8 s: with window_s 1 the exponential average reaches back 6 s, past the onset of the sine
    double t = feed(*a, t0, 8.0, 48000, 1000, -6, -6);
    Session s;
    JValue r = call(s, R"({"cmd":"get","uid":")" + a->uid + R"(","window_s":1.0})", t);
    CHECK(r.get("ok")->b);
    const JValue* snap = r.get("snapshot");
    CHECK(snap && snap->type == JValue::Object);
    if (snap) {
        NEAR(num(&snap->get("peak_db")->arr[0]), -6.0, 0.05);
        NEAR(num(&snap->get("peak_db")->arr[1]), -6.0, 0.05);
        NEAR(num(&snap->get("rms_db")->arr[0]), -9.0, 0.05);
        NEAR(num(snap->get("correlation")), 1.0, 0.001);
        NEAR(num(snap->get("width")), 0.0, 0.001);
        NEAR(num(snap->get("mid_db")), -9.0, 0.05);
        NEAR(num(snap->get("side_db")), -150.0, 0.01);
        NEAR(num(snap->get("crest_db")), 3.0, 0.05);
        const JValue* bands = snap->get("bands");
        CHECK(bands && bands->get("centers")->arr.size() == 31 && bands->get("mid")->arr.size() == 31);
        if (bands) {
            NEAR(num(&bands->get("centers")->arr[0]), 20.0, 0.01);
            NEAR(num(&bands->get("centers")->arr[30]), 20480.0, 0.1);
            // 1 kHz falls in band 17 (1015.9 Hz): the whole sine power, A²/2 = -9.0 dB
            NEAR(num(&bands->get("mid")->arr[17]), -9.0, 0.1);
            CHECK(num(&bands->get("mid")->arr[10]) < -80);
            CHECK(num(&bands->get("mid")->arr[24]) < -80);
            CHECK(num(&bands->get("side")->arr[17]) <= -140);
            printf("  sine 1 kHz -6 dBFS: peak %.1f, rms %.1f, band 1016 Hz %.1f, neighbours %.1f / %.1f, band 202 Hz %.1f, side %.1f\n",
                   num(&snap->get("peak_db")->arr[0]), num(&snap->get("rms_db")->arr[0]), num(&bands->get("mid")->arr[17]),
                   num(&bands->get("mid")->arr[16]), num(&bands->get("mid")->arr[18]), num(&bands->get("mid")->arr[10]),
                   num(&bands->get("side")->arr[17]));
        }
        CHECK(snap->get("lufs_i") == nullptr);  // the reference probe has no loudness
    }
    // fields filter
    r = call(s, R"({"cmd":"get","uid":")" + a->uid + R"(","fields":["peak_db"]})", t);
    CHECK(r.get("snapshot")->get("peak_db") && !r.get("snapshot")->get("rms_db"));

    // history: whole seconds of the server clock, oldest first
    r = call(s, R"({"cmd":"history","uid":")" + a->uid + R"(","seconds":60})", t + 0.5);
    CHECK(r.get("ok")->b);
    const JValue* sums = r.get("summaries");
    CHECK(sums && sums->arr.size() >= 3);
    if (sums && sums->arr.size() >= 3) {
        for (auto& x : sums->arr) CHECK(num(x.get("t")) == std::floor(num(x.get("t"))));
        NEAR(num(sums->arr[0].get("t")), std::floor(t0), 1e-9);
        NEAR(num(sums->arr[1].get("t")) - num(sums->arr[0].get("t")), 1.0, 1e-9);
        NEAR(num(&sums->arr[1].get("peak_db")->arr[0]), -6.0, 0.05);
        NEAR(num(&sums->arr[1].get("rms_db")->arr[0]), -9.0, 0.05);
        CHECK(sums->arr[1].get("bands_mid")->arr.size() == 31);
        NEAR(num(&sums->arr[2].get("bands_mid")->arr[17]), -9.0, 0.15);
    }

    // anti-phase on a second instance: correlation -1, all side
    auto b = hub.add();
    b->setFormat(2, 48000);
    t = feed(*b, t0, 2.0, 48000, 250, -12, -12, true);
    r = call(s, R"({"cmd":"get","uid":")" + b->uid + R"("})", t);
    snap = r.get("snapshot");
    NEAR(num(snap->get("correlation")), -1.0, 0.001);
    NEAR(num(snap->get("width")), 1.0, 0.001);
    NEAR(num(snap->get("side_db")), -15.0, 0.05);
    NEAR(num(&snap->get("bands")->get("side")->arr[11]), -15.0, 0.1);  // 254 Hz band

    // probe_id selection and ambiguity
    a->setProbeId(7);
    b->setProbeId(7);
    r = call(s, R"({"cmd":"get","probe_id":7})", t);
    CHECK(!r.get("ok")->b && r.get("code")->str == "bad_request");
    b->setProbeId(8);
    r = call(s, R"({"cmd":"get","probe_id":8,"rid":42})", t);
    CHECK(r.get("ok")->b && r.get("snapshot")->get("uid")->str == b->uid && num(r.get("rid")) == 42);

    // compare is not advertised by the reference probe
    r = call(s, R"({"cmd":"compare","a":")" + a->uid + R"(","b":")" + b->uid + R"("})", t);
    CHECK(!r.get("ok")->b && r.get("code")->str == "unsupported");

    hub.remove(a);
    hub.remove(b);
}

static void testCommands() {
    Hub& hub = Hub::get();
    auto a = hub.add();
    Session s;
    double now = monoNow();
    JValue r = call(s, R"({"cmd":"hello","rid":"x"})", now);
    CHECK(r.get("ok")->b && r.get("protocol")->str == "0.1" && r.get("rid")->str == "x");
    CHECK(r.get("product")->str == product().name && r.get("capabilities")->arr.size() == product().capabilities.size());
    r = call(s, R"({"cmd":"list"})", now);
    CHECK(r.get("instances")->arr.size() == 1);
    const JValue& id = r.get("instances")->arr[0];
    for (const char* k : {"uid", "probe_id", "track", "track_index", "track_color", "label", "channels", "sample_rate",
                          "latency_samples", "active", "signal"})
        CHECK(id.get(k) != nullptr);
    CHECK(id.get("track")->type == JValue::Null && num(id.get("latency_samples")) == 0);
    a->setTrack(true, "Bass", true, 3, true, 0x12ab34);
    r = call(s, R"({"cmd":"set_label","uid":")" + a->uid + R"(","label":"pre soothe2"})", now);
    CHECK(r.get("ok")->b);
    r = call(s, R"({"cmd":"list"})", now);
    const JValue& id2 = r.get("instances")->arr[0];
    CHECK(id2.get("track")->str == "Bass" && num(id2.get("track_index")) == 3 && id2.get("track_color")->str == "#12AB34");
    CHECK(id2.get("label")->str == "pre soothe2");
    r = call(s, R"({"cmd":"get","all":true})", now);
    CHECK(r.get("snapshots")->arr.size() == 1);
    r = call(s, R"({"cmd":"reset","all":true})", now);
    CHECK(r.get("ok")->b && r.get("reset")->arr.size() == 1);
    r = call(s, R"({"cmd":"subscribe","uid":")" + a->uid + R"(","hz":20,"fields":["peak_db"]})", now);
    CHECK(r.get("ok")->b && s.events && s.subs.size() == 1);
    std::string out;
    Protocol::dueEvents(s, now, out);
    JValue ev = parse(out.substr(0, out.find('\n')));
    CHECK(ev.get("event")->str == "snapshot" && ev.get("snapshot")->get("peak_db") && !ev.get("snapshot")->get("rms_db"));
    out.clear();
    Protocol::dueEvents(s, now + 0.01, out);
    CHECK(out.empty());  // 20 Hz: next one in 50 ms
    Protocol::dueEvents(s, now + 0.051, out);
    CHECK(!out.empty());
    r = call(s, R"({"cmd":"unsubscribe"})", now);
    CHECK(r.get("ok")->b && s.subs.empty() && !s.events);
    r = call(s, R"({"cmd":"get","uid":"nope"})", now);
    CHECK(r.get("code")->str == "not_found");
    r = call(s, R"({"cmd":"frobnicate"})", now);
    CHECK(r.get("code")->str == "unsupported");
    r = call(s, "not json", now);
    CHECK(r.get("code")->str == "bad_request");
    hub.remove(a);
}

static void testSilenceIdle() {
    // after 3.5 s of digital silence the engine stops running; bands still read silence
    Hub& hub = Hub::get();
    auto a = hub.add();
    double t0 = std::floor(monoNow()) - 30;
    double t = feed(*a, t0, 1.0, 48000, 1000, -6, -6);
    t = feed(*a, t, 6.0, 48000, 1000, -300, -300);
    Session s;
    JValue r = call(s, R"({"cmd":"get","uid":")" + a->uid + R"("})", t);
    const JValue* snap = r.get("snapshot");
    NEAR(num(&snap->get("peak_db")->arr[0]), -150.0, 0.01);
    CHECK(num(&snap->get("bands")->get("mid")->arr[17]) <= -140);
    t = feed(*a, t, 2.0, 48000, 1000, -20, -20);  // past the 341 ms FFT window filling up
    r = call(s, R"({"cmd":"get","uid":")" + a->uid + R"("})", t);
    NEAR(num(&r.get("snapshot")->get("bands")->get("mid")->arr[17]), -23.0, 0.2);
    hub.remove(a);
}

int main() {
    Hub::get().setServerEnabled(false);
    testJson();
    testJsonText();
    testSecondAlignment();
    testDelay();
    testOnsets();
    testPulse();
    testSignal();
    testThd();
    testRing();
    testSnapshot();
    testCommands();
    testSilenceIdle();
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
