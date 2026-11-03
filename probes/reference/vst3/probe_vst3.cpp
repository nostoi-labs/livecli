// SPDX-License-Identifier: MIT
#include "probe_vst3.h"

#include <cmath>
#include <cstring>

#include "base/source/fstreamer.h"
#include "hub.h"
#include "instance.h"
#include "pluginterfaces/base/ibstream.h"
#include "pluginterfaces/base/ustring.h"
#include "pluginterfaces/vst/ivsthostapplication.h"
#include "pluginterfaces/vst/ivstparameterchanges.h"
#include "pluginterfaces/vst/ivstprocesscontext.h"
#include "public.sdk/source/vst/vstparameters.h"

#ifndef PROBE_ID_AUTOMATABLE
#define PROBE_ID_AUTOMATABLE 1
#endif

namespace liveprobe {
namespace vst {

namespace {

std::string utf8(const Vst::TChar* s) {
    std::string o;
    if (!s) return o;
    for (size_t i = 0; s[i]; i++) {
        uint32_t c = (uint16_t)s[i];
        if (c >= 0xD800 && c < 0xDC00 && s[i + 1] >= 0xDC00 && s[i + 1] < 0xE000) {
            c = 0x10000 + ((c - 0xD800) << 10) + ((uint16_t)s[i + 1] - 0xDC00);
            i++;
        }
        if (c < 0x80) o += (char)c;
        else if (c < 0x800) { o += (char)(0xC0 | (c >> 6)); o += (char)(0x80 | (c & 0x3F)); }
        else if (c < 0x10000) {
            o += (char)(0xE0 | (c >> 12)); o += (char)(0x80 | ((c >> 6) & 0x3F)); o += (char)(0x80 | (c & 0x3F));
        } else {
            o += (char)(0xF0 | (c >> 18)); o += (char)(0x80 | ((c >> 12) & 0x3F));
            o += (char)(0x80 | ((c >> 6) & 0x3F)); o += (char)(0x80 | (c & 0x3F));
        }
    }
    return o;
}

const char* kSerialMsg = "liveprobe.serial";

}  // namespace

// ============================================================== processor

ProbeProcessor::ProbeProcessor(const FUID& controllerUID) { setControllerClass(controllerUID); }
ProbeProcessor::~ProbeProcessor() = default;

tresult PLUGIN_API ProbeProcessor::initialize(FUnknown* context) {
    tresult r = AudioEffect::initialize(context);
    if (r != kResultOk) return r;
    addAudioInput(STR16("Input"), Vst::SpeakerArr::kStereo);
    addAudioOutput(STR16("Output"), Vst::SpeakerArr::kStereo);
    if (FUnknownPtr<Vst::IHostApplication> app = context) {
        Vst::String128 name{};
        if (app->getName(name) == kResultOk) Hub::get().setHostName(utf8(name));
    }
    inst_ = Hub::get().add();
    inst_->setFormat(channels_, sampleRate_);
    return kResultOk;
}

tresult PLUGIN_API ProbeProcessor::terminate() {
    if (inst_) {
        inst_->stopPulse();
        inst_->stopSignal(true);
        Hub::get().remove(inst_);
        inst_.reset();
    }
    return AudioEffect::terminate();
}

void ProbeProcessor::sendSerial() {
    if (!inst_ || !peerConnection) return;
    if (auto msg = owned(allocateMessage())) {
        msg->setMessageID(kSerialMsg);
        msg->getAttributes()->setInt("serial", (int64)inst_->serial);
        sendMessage(msg);
    }
}

tresult PLUGIN_API ProbeProcessor::connect(Vst::IConnectionPoint* other) {
    tresult r = AudioEffect::connect(other);
    if (r == kResultTrue || r == kResultOk) sendSerial();
    return r;
}

tresult PLUGIN_API ProbeProcessor::setActive(TBool state) {
    if (state) sendSerial();
    else if (inst_) {  // never resume pulsing or a test signal after a deactivation
        inst_->stopPulse();
        inst_->stopSignal(true);
    }
    return AudioEffect::setActive(state);
}

tresult PLUGIN_API ProbeProcessor::setBusArrangements(Vst::SpeakerArrangement* inputs, int32 numIns,
                                                      Vst::SpeakerArrangement* outputs, int32 numOuts) {
    if (numIns != 1 || numOuts != 1 || inputs[0] != outputs[0]) return kResultFalse;
    const auto a = inputs[0];
    if (a != Vst::SpeakerArr::kStereo && a != Vst::SpeakerArr::kMono) return kResultFalse;
    removeAudioBusses();
    addAudioInput(STR16("Input"), a);
    addAudioOutput(STR16("Output"), a);
    channels_ = a == Vst::SpeakerArr::kMono ? 1 : 2;
    if (inst_) inst_->setFormat(channels_, sampleRate_);
    return kResultTrue;
}

tresult PLUGIN_API ProbeProcessor::canProcessSampleSize(int32 s) {
    return s == Vst::kSample32 ? kResultTrue : kResultFalse;
}

tresult PLUGIN_API ProbeProcessor::setupProcessing(Vst::ProcessSetup& setup) {
    sampleRate_ = setup.sampleRate;
    if (inst_) inst_->setFormat(channels_, sampleRate_);
    return AudioEffect::setupProcessing(setup);
}

tresult PLUGIN_API ProbeProcessor::process(Vst::ProcessData& data) {
    if (data.inputParameterChanges) {
        int32 n = data.inputParameterChanges->getParameterCount();
        for (int32 i = 0; i < n; i++) {
            Vst::IParamValueQueue* q = data.inputParameterChanges->getParameterData(i);
            if (!q || q->getParameterId() != kProbeIdTag) continue;
            int32 pts = q->getPointCount();
            Vst::ParamValue v;
            int32 off;
            if (pts > 0 && q->getPoint(pts - 1, off, v) == kResultTrue) {
                const int id = probeIdFromNormalized(v);
                probeId_.store(id, std::memory_order_relaxed);
                if (inst_) inst_->setProbeId(id);  // lock-free
            }
        }
    }
    if (data.numInputs < 1 || data.numOutputs < 1 || data.numSamples <= 0) return kResultOk;
    if (data.symbolicSampleSize != Vst::kSample32) return kResultFalse;  // refused in canProcessSampleSize
    Vst::AudioBusBuffers& in = data.inputs[0];
    Vst::AudioBusBuffers& out = data.outputs[0];
    const int32 n = data.numSamples;
    if (!in.channelBuffers32 || !out.channelBuffers32) return kResultOk;
    // pass-through, bit for bit
    for (int32 c = 0; c < out.numChannels; c++) {
        float* o = out.channelBuffers32[c];
        const float* src = c < in.numChannels ? in.channelBuffers32[c] : nullptr;
        if (!o) continue;
        if (src) {
            if (src != o) std::memcpy(o, src, sizeof(float) * (size_t)n);
        } else {
            std::memset(o, 0, sizeof(float) * (size_t)n);
        }
    }
    out.silenceFlags = in.numChannels >= out.numChannels ? in.silenceFlags : 0;
    if (inst_ && in.numChannels > 0 && in.channelBuffers32[0]) {
        const float* l = in.channelBuffers32[0];
        const float* r = in.numChannels > 1 ? in.channelBuffers32[1] : nullptr;
        // in-place hosts: the input pointers are the output pointers, which now hold the same samples
        int64 pos = -1;
        Transport tr;  // plain copies of the host's ProcessContext: no calls, no allocation
        if (const Vst::ProcessContext* pc = data.processContext) {
            pos = pc->projectTimeSamples;
            if (pc->state & Vst::ProcessContext::kPlaying) tr.flags |= Transport::kPlaying;
            if (pc->state & Vst::ProcessContext::kProjectTimeMusicValid) {
                tr.flags |= Transport::kMusicValid;
                tr.ppq = pc->projectTimeMusic;
            }
            if (pc->state & Vst::ProcessContext::kTempoValid) {
                tr.flags |= Transport::kTempoValid;
                tr.tempo = pc->tempo;
            }
            if ((pc->state & Vst::ProcessContext::kCycleActive) && (pc->state & Vst::ProcessContext::kCycleValid)) {
                tr.flags |= Transport::kCycleActive;
                tr.cycleStart = pc->cycleStartMusic;
                tr.cycleEnd = pc->cycleEndMusic;
            }
        }
        const double now = monoNow();
        // §5.11: test impulses only on an explicit `pulse` request; the ring then gets what really goes out
        // §5.12 / §5.11: test signals and impulses only on an explicit request
        const bool sig = inst_->renderSignal(out.channelBuffers32, out.numChannels, n, tr, pos, sampleRate_, now);
        const bool pul = inst_->renderPulse(out.channelBuffers32, out.numChannels, n, tr, sampleRate_, now);
        if (sig || pul) {
            out.silenceFlags = 0;
            l = out.channelBuffers32[0];
            r = out.numChannels > 1 ? out.channelBuffers32[1] : nullptr;
        }
        inst_->pushAudio(l, r, n, sampleRate_, pos, now, &tr);
    }
    return kResultOk;
}

tresult PLUGIN_API ProbeProcessor::setState(IBStream* state) {
    if (!state) return kResultFalse;
    IBStreamer s(state, kLittleEndian);
    int32 version = 0, id = 0;
    if (!s.readInt32(version)) return kResultFalse;
    if (!s.readInt32(id)) return kResultFalse;
    id = id < 0 ? 0 : id > 999 ? 999 : id;
    probeId_.store(id);
    if (inst_) inst_->setProbeId(id);
    return kResultOk;
}

tresult PLUGIN_API ProbeProcessor::getState(IBStream* state) {
    if (!state) return kResultFalse;
    IBStreamer s(state, kLittleEndian);
    s.writeInt32(kStateVersion);
    s.writeInt32(probeId_.load());
    return kResultOk;
}

// ============================================================== controller

ProbeController::ProbeController() = default;
ProbeController::~ProbeController() = default;

tresult PLUGIN_API ProbeController::initialize(FUnknown* context) {
    tresult r = EditController::initialize(context);
    if (r != kResultOk) return r;
    int32 flags = PROBE_ID_AUTOMATABLE ? Vst::ParameterInfo::kCanAutomate : Vst::ParameterInfo::kNoFlags;
    auto* p = new Vst::RangeParameter(STR16("Probe ID"), kProbeIdTag, nullptr, 0, 999, 0, 999, flags);
    p->setPrecision(0);
    parameters.addParameter(p);
    return kResultOk;
}

tresult PLUGIN_API ProbeController::terminate() {
    inst_.reset();
    return EditController::terminate();
}

tresult PLUGIN_API ProbeController::setComponentState(IBStream* state) {
    if (!state) return kResultFalse;
    IBStreamer s(state, kLittleEndian);
    int32 version = 0, id = 0;
    if (!s.readInt32(version) || !s.readInt32(id)) return kResultFalse;
    id = id < 0 ? 0 : id > 999 ? 999 : id;
    EditController::setParamNormalized(kProbeIdTag, id / 999.0);
    return kResultOk;
}

tresult PLUGIN_API ProbeController::setParamNormalized(Vst::ParamID tag, Vst::ParamValue value) {
    tresult r = EditController::setParamNormalized(tag, value);
    if (tag == kProbeIdTag)
        if (auto i = inst_.lock()) i->setProbeId(probeIdFromNormalized(value));
    return r;
}

tresult PLUGIN_API ProbeController::notify(Vst::IMessage* message) {
    if (message && message->getMessageID() && std::strcmp(message->getMessageID(), kSerialMsg) == 0) {
        int64 serial = 0;
        if (message->getAttributes() && message->getAttributes()->getInt("serial", serial) == kResultOk) {
            inst_ = Hub::get().findSerial((uint64_t)serial);
            apply();
        }
        return kResultOk;
    }
    return EditController::notify(message);
}

tresult PLUGIN_API ProbeController::setChannelContextInfos(Vst::IAttributeList* list) {
    if (!list) return kResultFalse;
    // Live 12 sends only the keys that changed (on load just the color, after a rename just the
    // name): update what is present, keep the rest
    Vst::String128 name{};
    if (list->getString(Vst::ChannelContext::kChannelNameKey, name, sizeof(name)) == kResultTrue) {
        hasName_ = true;
        name_ = utf8(name);
    }
    int64 v = 0;
    if (list->getInt(Vst::ChannelContext::kChannelIndexKey, v) == kResultTrue) {
        hasIndex_ = true;
        index_ = v;
    }
    if (list->getInt(Vst::ChannelContext::kChannelColorKey, v) == kResultTrue) {
        hasColor_ = true;
        color_ = (uint32_t)(v & 0xFFFFFF);
    }
    apply();
    return kResultTrue;
}

void ProbeController::apply() {
    auto i = inst_.lock();
    if (!i) return;
    if (hasName_ || hasIndex_ || hasColor_) i->setTrack(hasName_, name_, hasIndex_, index_, hasColor_, color_);
    if (auto* p = parameters.getParameter(kProbeIdTag)) i->setProbeId(probeIdFromNormalized(p->getNormalized()));
}

}  // namespace vst
}  // namespace liveprobe
