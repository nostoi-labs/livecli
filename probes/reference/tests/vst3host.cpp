// SPDX-License-Identifier: MIT
// Minimal real-time VST3 test host for probe plug-ins (no audio device, no UI).
// Loads the bundle, creates one instance per signal, gives each a track name/index/color through
// IInfoListener and a Probe ID through the parameter, then plays the signals in real time,
// checking on every block that the output equals the input bit for bit.
//
//   probe_vst3host <plugin.vst3> <seconds> <signal> [<signal> ...]
//   signal: [<track name>=]<kind>[@inplace][@mono]
//   kind:   sine:<hz>:<dBFS> | sinel:<hz>:<dBFS> (left only) | noise:<dBFS> | anti:<dBFS> | silence
//           | eqnoise:<dBFS>:<hz>:<gain dB> (the same noise as `noise`, through an RBJ peaking EQ, Q 1)
//   @inplace: input and output share the same buffers (as some hosts do)
//   @mono:    mono bus arrangement (1 channel)
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
    double phase = 0;
    std::mt19937 rng{1234};
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
        s = s.substr(0, at);
    }
    std::vector<std::string> p;
    size_t a = 0, b;
    while ((b = s.find(':', a)) != std::string::npos) { p.push_back(s.substr(a, b - a)); a = b + 1; }
    p.push_back(s.substr(a));
    g.kind = p[0];
    if ((g.kind == "sine" || g.kind == "sinel") && p.size() >= 3) { g.hz = atof(p[1].c_str()); g.db = atof(p[2].c_str()); }
    else if ((g.kind == "noise" || g.kind == "anti") && p.size() >= 2) g.db = atof(p[1].c_str());
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
    auto module = VST3::Hosting::Module::create(argv[1], err);
    if (!module) { fprintf(stderr, "load failed: %s\n", err.c_str()); return 1; }
    auto factory = module->getFactory();
    static HostApplication host;
    PluginContextFactory::instance().setPluginContext(&host);
    VST3::Hosting::ClassInfo info;
    bool found = false;
    for (auto& ci : factory.classInfos())
        if (ci.category() == kVstAudioEffectClass) { info = ci; found = true; break; }
    if (!found) { fprintf(stderr, "no audio effect class\n"); return 1; }
    printf("plugin: %s (%s) vendor %s version %s\n", info.name().c_str(), info.subCategoriesString().c_str(),
           info.vendor().c_str(), info.version().c_str());

    std::vector<std::unique_ptr<Inst>> insts;
    for (int k = 3; k < argc; k++) {
        auto in = std::make_unique<Inst>();
        in->sig = parseSignal(argv[k]);
        in->provider = owned(new PlugProvider(factory, info, true));
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
            auto attrs = HostAttributeList::make();
            std::string name = in->sig.name.empty() ? "Test " + std::to_string(k - 2) + " " + argv[k] : in->sig.name;
            auto u16 = VST3::StringConvert::convert(name);
            attrs->setString(ChannelContext::kChannelNameKey, (const TChar*)u16.data());
            attrs->setInt(ChannelContext::kChannelIndexKey, k - 2);
            attrs->setInt(ChannelContext::kChannelColorKey, (int64)0xFFFF8800u + (k - 2));
            il->setChannelContextInfos(attrs);
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

    ProcessContext ctx{};
    ctx.sampleRate = sr;
    ctx.state = ProcessContext::kPlaying;
    auto t0 = std::chrono::steady_clock::now();
    const long blocks = (long)(seconds * sr / block);
    for (long b = 0; b < blocks; b++) {
        ctx.projectTimeSamples = b * block;
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
    for (auto& in : insts) {
        bad += in->mismatches;
        in->proc->setProcessing(false);
        in->comp->setActive(false);
        in->data.unprepare();
    }
    printf("bit-exact: %s (%ld mismatching blocks of %ld)\n", bad == 0 ? "yes" : "NO", bad, blocks * (long)insts.size());
    insts.clear();
    return bad == 0 ? 0 : 1;
}
