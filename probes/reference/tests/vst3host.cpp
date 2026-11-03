// SPDX-License-Identifier: MIT
// Minimal real-time VST3 test host for probe plug-ins (no audio device, no UI).
// Loads the bundle, creates one instance per signal, gives each a track name/index/color through
// IInfoListener and a Probe ID through the parameter, then plays the signals in real time,
// checking on every block that the output equals the input bit for bit.
//
//   probe_vst3host <plugin.vst3>[,<plugin2.vst3>…] <seconds> <signal> [<signal> ...]
//   signal: [<track name>=]<kind>[@inplace][@mono]
//   kind:   sine:<hz>:<dBFS> | sinel:<hz>:<dBFS> (left only) | noise:<dBFS> | anti:<dBFS> | silence
//           | eqnoise:<dBFS>:<hz>:<gain dB> (the same noise as `noise`, through an RBJ peaking EQ, Q 1)
//           | dnoise:<dBFS>:<ms> (the same noise as `noise`, delayed by <ms>; negative <ms>: also inverted)
//           | hsine:<hz>:<dBFS>:<H2 dBc>:<H3 dBc> (a sine with two harmonics, for thd)
//           | clicks:<dBFS>:<ms> (a click on every 1/16 of the host tempo, <ms> late: 2 kHz cosine burst, 5 ms decay)
//   environment: PROBE_HOST_TEMPO (bpm, default 120), PROBE_HOST_TRANSPORT = playing (default) | stopped |
//                nomusic (no song position) | loop (cycle over the first 4 quarter notes, wrapping inside blocks)
//   @inplace: input and output share the same buffers (as some hosts do)
//   @mono:    mono bus arrangement (1 channel)
//   @p<N>:    instance of the N-th bundle when <plugin.vst3> is a comma-separated list
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <random>
#include <string>
#include <thread>
#include <vector>

#include "pluginterfaces/vst/ivstaudioprocessor.h"
#include "pluginterfaces/vst/ivstchannelcontextinfo.h"
#include "pluginterfaces/vst/ivsteditcontroller.h"
#include "pluginterfaces/vst/ivstprocesscontext.h"
#include "public.sdk/source/vst/hosting/hostclasses.h"
#include "public.sdk/source/vst/hosting/module.h"
#include "public.sdk/source/vst/hosting/parameterchanges.h"
#include "public.sdk/source/vst/hosting/plugprovider.h"
#include "public.sdk/source/vst/hosting/processdata.h"
#include "public.sdk/source/vst/utility/stringconvert.h"

using namespace Steinberg;
using namespace Steinberg::Vst;

struct Signal {
    std::string kind, name;
    double hz = 1000, db = -6, gain = 0;
    bool inplace = false, mono = false;
    int plugin = 1;
    double phase = 0;
    std::mt19937 rng{1234};
    std::vector<float> dl, dr;  // dnoise delay lines
    long clickPos = 0;
    double h2 = -400, h3 = -400;
    double tempo = 120;
    size_t dpos = 0;
    // peaking EQ state (direct form I, per channel)
    double b0 = 1, b1 = 0, b2 = 0, a1 = 0, a2 = 0, xs[2][2] = {}, ys[2][2] = {};
    void prepareEq(double sr) {
        double A = std::pow(10.0, gain / 40.0), w = 2 * M_PI * hz / sr, al = std::sin(w) / 2.0;  // Q = 1
        double a0 = 1 + al / A;
        b0 = (1 + al * A) / a0; b1 = -2 * std::cos(w) / a0; b2 = (1 - al * A) / a0;
        a1 = -2 * std::cos(w) / a0; a2 = (1 - al / A) / a0;
    }
    float eq(int c, double x) {
        double y = b0 * x + b1 * xs[c][0] + b2 * xs[c][1] - a1 * ys[c][0] - a2 * ys[c][1];
        xs[c][1] = xs[c][0]; xs[c][0] = x; ys[c][1] = ys[c][0]; ys[c][0] = y;
        return (float)y;
    }
    void fill(float* l, float* r, int n, double sr) {
        double a = std::pow(10.0, db / 20.0);
        std::normal_distribution<float> g(0.f, 1.f);
        for (int i = 0; i < n; i++) {
            float x = 0, y = 0;
            if (kind == "sine" || kind == "sinel") {
                x = (float)(a * std::sin(phase));
                y = kind == "sine" ? x : 0.f;
                phase += 2 * M_PI * hz / sr;
                if (phase > 2 * M_PI) phase -= 2 * M_PI;
            } else if (kind == "noise" || kind == "eqnoise") {
                x = (float)(a * g(rng));
                y = (float)(a * g(rng));
                if (kind == "eqnoise") { x = eq(0, x); y = eq(1, y); }
            } else if (kind == "dnoise") {
                float nx = (float)(a * g(rng)), nyv = (float)(a * g(rng));
                if (dl.empty()) { size_t d = (size_t)std::lround(std::fabs(hz) * sr / 1000.0); dl.assign(d + 1, 0.f); dr.assign(d + 1, 0.f); }
                dl[dpos] = nx; dr[dpos] = nyv;
                size_t rd = (dpos + 1) % dl.size();
                float sgn = hz < 0 ? -1.f : 1.f;
                x = sgn * dl[rd]; y = sgn * dr[rd];
                dpos = rd;
            } else if (kind == "hsine") {
                x = y = (float)(a * (std::sin(phase) + std::pow(10.0, h2 / 20) * std::sin(2 * phase + 1.1) +
                                     std::pow(10.0, h3 / 20) * std::sin(3 * phase + 2.2)));
                phase += 2 * M_PI * hz / sr;
                if (phase > 2 * M_PI) phase -= 2 * M_PI;
            } else if (kind == "clicks") {
                // sample index n since the start; 1/16 grid of `tempo`, clicks `hz` ms late
                const double grid = sr * 60.0 / tempo / 4.0;
                const double n = (double)clickPos++ - hz * sr / 1000.0;
                if (n >= 0) {
                    const double k = std::floor(n / grid), dt = (n - k * grid) / sr;  // seconds since the click
                    if (dt < 0.03) x = y = (float)(a * std::cos(2 * M_PI * 2000 * dt) * std::exp(-dt / 0.005));
                }
            } else if (kind == "anti") {
                x = (float)(a * g(rng));
                y = -x;
            }
            l[i] = x;
            if (r) r[i] = y;
        }
    }
};

static Signal parseSignal(std::string s) {
    Signal g;
    size_t eq = s.find('=');
    if (eq != std::string::npos) { g.name = s.substr(0, eq); s = s.substr(eq + 1); }
    size_t at;
    while ((at = s.rfind('@')) != std::string::npos) {
        std::string f = s.substr(at + 1);
        if (f == "inplace") g.inplace = true;
        if (f == "mono") g.mono = true;
        if (f.size() > 1 && f[0] == 'p') g.plugin = atoi(f.c_str() + 1);
        s = s.substr(0, at);
    }
    std::vector<std::string> p;
    size_t a = 0, b;
    while ((b = s.find(':', a)) != std::string::npos) { p.push_back(s.substr(a, b - a)); a = b + 1; }
    p.push_back(s.substr(a));
    g.kind = p[0];
    if ((g.kind == "sine" || g.kind == "sinel") && p.size() >= 3) { g.hz = atof(p[1].c_str()); g.db = atof(p[2].c_str()); }
    else if ((g.kind == "noise" || g.kind == "anti") && p.size() >= 2) g.db = atof(p[1].c_str());
    else if (g.kind == "hsine" && p.size() >= 5) {
        g.hz = atof(p[1].c_str()); g.db = atof(p[2].c_str()); g.h2 = atof(p[3].c_str()); g.h3 = atof(p[4].c_str());
    }
    else if (g.kind == "clicks" && p.size() >= 3) { g.db = atof(p[1].c_str()); g.hz = atof(p[2].c_str()); }
    else if (g.kind == "dnoise" && p.size() >= 3) { g.db = atof(p[1].c_str()); g.hz = atof(p[2].c_str()); }
    else if (g.kind == "eqnoise" && p.size() >= 4) {
        g.db = atof(p[1].c_str()); g.hz = atof(p[2].c_str()); g.gain = atof(p[3].c_str());
    }
    return g;
}

struct Inst {
    IPtr<PlugProvider> provider;
    IPtr<IComponent> comp;
    IPtr<IAudioProcessor> proc;
    IPtr<IEditController> ctrl;
    HostProcessData data;
    ParameterChanges inChanges;
    Signal sig;
    std::vector<float> inL, inR, outL, outR, refL, refR;
    int channels = 2;
    long mismatches = 0;
};

int main(int argc, char** argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: %s <plugin.vst3> <seconds> <signal>...\n", argv[0]);
        return 2;
    }
    const double seconds = atof(argv[2]);
    const double sr = 48000;
    const int block = 512;
    std::string err;
    // one or more bundles, comma separated: signals pick one with @p2, @p3 … (default @p1)
    std::vector<VST3::Hosting::Module::Ptr> modules;
    std::vector<VST3::Hosting::ClassInfo> infos;
    static HostApplication host;
    PluginContextFactory::instance().setPluginContext(&host);
    {
        std::string list = argv[1];
        size_t a = 0;
        while (a <= list.size()) {
            size_t b = list.find(',', a);
            if (b == std::string::npos) b = list.size();
            std::string path = list.substr(a, b - a);
            a = b + 1;
            if (path.empty()) continue;
            auto module = VST3::Hosting::Module::create(path, err);
            if (!module) { fprintf(stderr, "load failed: %s\n", err.c_str()); return 1; }
            bool found = false;
            for (auto& ci : module->getFactory().classInfos())
                if (ci.category() == kVstAudioEffectClass) {
                    printf("plugin %zu: %s (%s) vendor %s version %s\n", modules.size() + 1, ci.name().c_str(),
                           ci.subCategoriesString().c_str(), ci.vendor().c_str(), ci.version().c_str());
                    infos.push_back(ci);
                    found = true;
                    break;
                }
            if (!found) { fprintf(stderr, "no audio effect class in %s\n", path.c_str()); return 1; }
            modules.push_back(module);
        }
    }

    std::vector<std::unique_ptr<Inst>> insts;
    for (int k = 3; k < argc; k++) {
        auto in = std::make_unique<Inst>();
        in->sig = parseSignal(argv[k]);
        const size_t pi = std::min(modules.size(), (size_t)std::max(1, in->sig.plugin)) - 1;
        in->provider = owned(new PlugProvider(modules[pi]->getFactory(), infos[pi], true));
        if (!in->provider->initialize()) { fprintf(stderr, "initialize failed\n"); return 1; }
        in->comp = in->provider->getComponentPtr();
        in->ctrl = in->provider->getControllerPtr();
        in->proc = FUnknownPtr<IAudioProcessor>(in->comp);
        if (!in->proc || !in->ctrl) { fprintf(stderr, "no processor/controller\n"); return 1; }
        if (in->proc->canProcessSampleSize(kSample64) == kResultTrue) printf("  note: accepts 64-bit samples\n");
        else printf("  64-bit samples refused (32-bit only)\n");
        in->channels = in->sig.mono ? 1 : 2;
        SpeakerArrangement st = in->sig.mono ? SpeakerArr::kMono : SpeakerArr::kStereo;
        if (in->proc->setBusArrangements(&st, 1, &st, 1) != kResultTrue) { fprintf(stderr, "bus arrangement refused\n"); return 1; }
        ProcessSetup setup{kRealtime, kSample32, block, sr};
        if (in->proc->setupProcessing(setup) != kResultOk) { fprintf(stderr, "setupProcessing failed\n"); return 1; }
        if (in->sig.kind == "eqnoise") in->sig.prepareEq(sr);
        in->comp->activateBus(kAudio, kInput, 0, true);
        in->comp->activateBus(kAudio, kOutput, 0, true);
        in->comp->setActive(true);
        in->data.prepare(*in->comp, 0, kSample32);
        in->data.numSamples = block;
        in->inL.resize(block); in->inR.resize(block); in->outL.resize(block); in->outR.resize(block);
        in->refL.resize(block); in->refR.resize(block);
        float* ins[2] = {in->inL.data(), in->inR.data()};
        float* outs[2] = {in->outL.data(), in->outR.data()};
        if (in->sig.inplace) { outs[0] = ins[0]; outs[1] = ins[1]; }
        in->data.setChannelBuffers(kInput, 0, ins, in->channels);
        in->data.setChannelBuffers(kOutput, 0, in->sig.inplace ? ins : outs, in->channels);
        in->data.inputParameterChanges = &in->inChanges;

        // latency must be 0
        printf("instance %d: latency %u tail %u\n", k - 2, in->proc->getLatencySamples(), in->proc->getTailSamples());
        // track info, like Live does
        if (FUnknownPtr<ChannelContext::IInfoListener> il{in->ctrl.get()}) {
            // like Live 12: every call carries only the keys that changed (color on load, then the name…)
            std::string name = in->sig.name.empty() ? "Test " + std::to_string(k - 2) + " " + argv[k] : in->sig.name;
            auto u16 = VST3::StringConvert::convert(name);
            auto color = HostAttributeList::make();
            color->setInt(ChannelContext::kChannelColorKey, (int64)0xFFFF8800u + (k - 2));
            il->setChannelContextInfos(color);
            auto named = HostAttributeList::make();
            named->setString(ChannelContext::kChannelNameKey, (const TChar*)u16.data());
            il->setChannelContextInfos(named);
            auto index = HostAttributeList::make();
            index->setInt(ChannelContext::kChannelIndexKey, k - 2);
            il->setChannelContextInfos(index);
        } else {
            printf("controller has no IInfoListener!\n");
        }
        // Probe ID = 100 + k through the controller and the processor, like a host parameter change
        ParamValue norm = (100 + (k - 2)) / 999.0;
        in->ctrl->setParamNormalized(0, norm);
        int32 idx;
        if (auto* q = in->inChanges.addParameterData(0, idx)) { int32 pi; q->addPoint(0, norm, pi); }
        ParameterInfo pinfo{};
        if (in->ctrl->getParameterCount() == 1 && in->ctrl->getParameterInfo(0, pinfo) == kResultOk) {
            std::string t = VST3::StringConvert::convert(pinfo.title);
            String128 disp{};
            in->ctrl->getParamStringByValue(0, norm, disp);
            printf("  param '%s' steps %d flags %d display '%s'\n", t.c_str(), pinfo.stepCount, pinfo.flags,
                   VST3::StringConvert::convert(disp).c_str());
        } else {
            printf("  parameter count %d\n", in->ctrl->getParameterCount());
        }
        in->proc->setProcessing(true);
        insts.push_back(std::move(in));
    }
    printf("running %.0f s with %zu instances\n", seconds, insts.size());
    fflush(stdout);

    const char* tempoEnv = getenv("PROBE_HOST_TEMPO");
    const double tempo = tempoEnv ? atof(tempoEnv) : 120.0;
    const std::string mode = getenv("PROBE_HOST_TRANSPORT") ? getenv("PROBE_HOST_TRANSPORT") : "playing";
    for (auto& in : insts) in->sig.tempo = tempo;
    ProcessContext ctx{};
    ctx.sampleRate = sr;
    ctx.tempo = tempo;
    ctx.state = ProcessContext::kTempoValid;
    if (mode != "stopped") ctx.state |= ProcessContext::kPlaying;
    if (mode != "nomusic") ctx.state |= ProcessContext::kProjectTimeMusicValid;
    if (mode == "loop") {
        ctx.state |= ProcessContext::kCycleActive | ProcessContext::kCycleValid;
        ctx.cycleStartMusic = 0;
        ctx.cycleEndMusic = 4;
    }
    printf("transport %s, %.1f bpm\n", mode.c_str(), tempo);
    auto t0 = std::chrono::steady_clock::now();
    const long blocks = (long)(seconds * sr / block);
    for (long b = 0; b < blocks; b++) {
        ctx.projectTimeSamples = b * block;
        ctx.projectTimeMusic = b * block / sr * tempo / 60.0;
        if (mode == "loop") ctx.projectTimeMusic = std::fmod(ctx.projectTimeMusic, 4.0);
        for (auto& in : insts) {
            const bool st = in->channels == 2;
            in->sig.fill(in->inL.data(), st ? in->inR.data() : nullptr, block, sr);
            std::memcpy(in->refL.data(), in->inL.data(), block * sizeof(float));
            std::memcpy(in->refR.data(), in->inR.data(), block * sizeof(float));
            if (!in->sig.inplace) {  // poison the outputs: the plug-in must write every sample
                std::fill(in->outL.begin(), in->outL.end(), 12345.f);
                std::fill(in->outR.begin(), in->outR.end(), 12345.f);
            }
            in->data.processContext = &ctx;
            in->proc->process(in->data);
            in->inChanges.clearQueue();
            const float* oL = in->sig.inplace ? in->inL.data() : in->outL.data();
            const float* oR = in->sig.inplace ? in->inR.data() : in->outR.data();
            if (std::memcmp(in->refL.data(), oL, block * sizeof(float)) != 0 ||
                (st && std::memcmp(in->refR.data(), oR, block * sizeof(float)) != 0) ||
                std::memcmp(in->refL.data(), in->inL.data(), block * sizeof(float)) != 0)  // input untouched
                in->mismatches++;
        }
        std::this_thread::sleep_until(t0 + std::chrono::duration<double>((b + 1) * block / sr));
    }
    long bad = 0;
    for (size_t k = 0; k < insts.size(); k++) {
        auto& in = insts[k];
        if (in->mismatches) printf("instance %zu (%s): %ld blocks differ from the input\n", k + 1, in->sig.name.c_str(), in->mismatches);
        bad += in->mismatches;
        in->proc->setProcessing(false);
        in->comp->setActive(false);
        in->data.unprepare();
    }
    printf("bit-exact: %s (%ld mismatching blocks of %ld)\n", bad == 0 ? "yes" : "NO", bad, blocks * (long)insts.size());
    insts.clear();
    return bad == 0 ? 0 : 1;
}
