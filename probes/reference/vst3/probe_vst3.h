// SPDX-License-Identifier: MIT
// Live Probe VST3 container: stereo pass-through, zero latency, one "Probe ID" parameter,
// track name/index/color from IInfoListener. Shared by every probe product.
#pragma once

#include <atomic>
#include <memory>
#include <string>

#include "base/source/fstring.h"
#include "pluginterfaces/vst/ivstchannelcontextinfo.h"
#include "public.sdk/source/vst/vstaudioeffect.h"
#include "public.sdk/source/vst/vsteditcontroller.h"

namespace liveprobe {

class Instance;

namespace vst {

using namespace Steinberg;

enum : Vst::ParamID { kProbeIdTag = 0 };
constexpr int kStateVersion = 1;

class ProbeProcessor : public Vst::AudioEffect {
public:
    explicit ProbeProcessor(const FUID& controllerUID);
    ~ProbeProcessor() override;

    tresult PLUGIN_API initialize(FUnknown* context) override;
    tresult PLUGIN_API terminate() override;
    tresult PLUGIN_API connect(Vst::IConnectionPoint* other) override;
    tresult PLUGIN_API setActive(TBool state) override;
    tresult PLUGIN_API setBusArrangements(Vst::SpeakerArrangement* inputs, int32 numIns,
                                          Vst::SpeakerArrangement* outputs, int32 numOuts) override;
    tresult PLUGIN_API canProcessSampleSize(int32 symbolicSampleSize) override;
    tresult PLUGIN_API setupProcessing(Vst::ProcessSetup& setup) override;
    uint32 PLUGIN_API getLatencySamples() override { return 0; }
    uint32 PLUGIN_API getTailSamples() override { return Vst::kNoTail; }
    tresult PLUGIN_API process(Vst::ProcessData& data) override;
    tresult PLUGIN_API setState(IBStream* state) override;
    tresult PLUGIN_API getState(IBStream* state) override;

private:
    void sendSerial();
    std::shared_ptr<Instance> inst_;
    int channels_ = 2;
    double sampleRate_ = 48000;
    std::atomic<int> probeId_{0};  // written by process(), read by getState()
};

class ProbeController : public Vst::EditController, public Vst::ChannelContext::IInfoListener {
public:
    ProbeController();
    ~ProbeController() override;

    tresult PLUGIN_API initialize(FUnknown* context) override;
    tresult PLUGIN_API terminate() override;
    tresult PLUGIN_API setComponentState(IBStream* state) override;
    tresult PLUGIN_API setParamNormalized(Vst::ParamID tag, Vst::ParamValue value) override;
    tresult PLUGIN_API notify(Vst::IMessage* message) override;
    IPlugView* PLUGIN_API createView(FIDString) override { return nullptr; }

    // IInfoListener
    tresult PLUGIN_API setChannelContextInfos(Vst::IAttributeList* list) override;

    OBJ_METHODS(ProbeController, EditController)
    DEFINE_INTERFACES
        DEF_INTERFACE(Vst::ChannelContext::IInfoListener)
    END_DEFINE_INTERFACES(EditController)
    REFCOUNT_METHODS(EditController)

private:
    void apply();
    std::weak_ptr<Instance> inst_;
    bool hasName_ = false, hasIndex_ = false, hasColor_ = false;
    std::string name_;
    long long index_ = 0;
    uint32_t color_ = 0;
};

/// Normalized parameter value <-> Probe ID (0…999).
inline int probeIdFromNormalized(double v) {
    int i = (int)(v * 999.0 + 0.5);
    return i < 0 ? 0 : i > 999 ? 999 : i;
}

}  // namespace vst
}  // namespace liveprobe
