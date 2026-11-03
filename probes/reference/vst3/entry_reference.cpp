// SPDX-License-Identifier: MIT
// Reference Probe: VST3 factory.
#include "hub.h"
#include "probe_vst3.h"
#include "public.sdk/source/main/moduleinit.h"
#include "public.sdk/source/main/pluginfactory.h"

#ifndef PROBE_VERSION
#define PROBE_VERSION "0.1.0"
#endif

using namespace Steinberg;

static const FUID kProcessorUID(0x22493740, 0x35D041E3, 0x86A91794, 0x15DC68B4);
static const FUID kControllerUID(0xEB915529, 0xAE194C61, 0xB046EAC1, 0x9B98521E);

static FUnknown* createProcessor(void*) {
    return (Vst::IAudioProcessor*)new liveprobe::vst::ProbeProcessor(kControllerUID);
}
static FUnknown* createController(void*) {
    return (Vst::IEditController*)new liveprobe::vst::ProbeController();
}

// bundle unload: no probe thread may outlive the code it runs
static Steinberg::ModuleTerminator stopProbeThreads([] { liveprobe::Hub::get().shutdown(); });

BEGIN_FACTORY_DEF("LiveCLI", "", "")

DEF_CLASS2(INLINE_UID_FROM_FUID(kProcessorUID), PClassInfo::kManyInstances, kVstAudioEffectClass, "Reference Probe",
           Vst::kDistributable, "Fx|Analyzer", PROBE_VERSION, kVstVersionString, createProcessor)

DEF_CLASS2(INLINE_UID_FROM_FUID(kControllerUID), PClassInfo::kManyInstances, kVstComponentControllerClass,
           "Reference Probe Controller", 0, "", PROBE_VERSION, kVstVersionString, createController)

END_FACTORY
