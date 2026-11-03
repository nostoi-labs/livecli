// SPDX-License-Identifier: MIT
// Unit tests for the probe core and the reference DSP (no VST3, no sockets).
#include <cmath>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "hub.h"
#include "instance.h"
#include "json.h"
#include "reference_engine.h"
#include "ring.h"
#include "server.h"

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
    testRing();
    testSnapshot();
    testCommands();
    testSilenceIdle();
    printf("%d checks, %d failures\n", checks, failures);
    return failures ? 1 : 0;
}
