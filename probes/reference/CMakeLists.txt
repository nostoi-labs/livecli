# SPDX-License-Identifier: MIT
# Reference Probe (VST3) and the shared Live Probe core.
#
#   cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DVST3_SDK_ROOT=/path/to/vst3sdk
#   cmake --build build
#   ./build/probe_tests
#
# Without VST3_SDK_ROOT only the core, the DSP and the unit tests are built.
cmake_minimum_required(VERSION 3.20)

set(CMAKE_OSX_DEPLOYMENT_TARGET 12.0 CACHE STRING "")
if(APPLE AND NOT CMAKE_OSX_ARCHITECTURES)
    set(CMAKE_OSX_ARCHITECTURES arm64 CACHE STRING "")
endif()

project(reference-probe VERSION 0.1.0 LANGUAGES C CXX)
if(APPLE)
    enable_language(OBJCXX)
endif()

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

set(VST3_SDK_ROOT "" CACHE PATH "Steinberg VST3 SDK (3.8 or later, MIT)")
# Protocol §6 asks for "automatable off", but Live hides VST3 parameters without kCanAutomate from its
# device parameter list (and so from the Live API the CLI uses to set the ID). ON until verified in Live.
option(PROBE_ID_AUTOMATABLE "Mark the Probe ID parameter automatable (Live lists only automatable parameters)" ON)
set(PROBE_VERSION "${PROJECT_VERSION}")

# ---- core (no SDK) -----------------------------------------------------------
add_library(liveprobe_core STATIC
    core/json.cpp
    core/instance.cpp
    core/hub.cpp
    core/server.cpp
)
target_include_directories(liveprobe_core PUBLIC core)
find_package(Threads REQUIRED)
target_link_libraries(liveprobe_core PUBLIC Threads::Threads)

# ---- reference DSP -----------------------------------------------------------
add_library(reference_engine STATIC
    engine/reference_engine.cpp
    engine/product_reference.cpp
)
target_include_directories(reference_engine PUBLIC engine)
target_link_libraries(reference_engine PUBLIC liveprobe_core)
target_compile_definitions(reference_engine PRIVATE PROBE_VERSION="${PROBE_VERSION}")
if(APPLE)
    target_link_libraries(reference_engine PUBLIC "-framework Accelerate")
endif()

# ---- unit tests ----------------------------------------------------------------
add_executable(probe_tests tests/test_core.cpp)
target_link_libraries(probe_tests PRIVATE reference_engine liveprobe_core)
enable_testing()
add_test(NAME probe_tests COMMAND probe_tests)

# ---- VST3 plug-in --------------------------------------------------------------
if(VST3_SDK_ROOT)
    set(SMTG_ENABLE_VST3_HOSTING_EXAMPLES OFF CACHE BOOL "" FORCE)
    set(SMTG_ENABLE_VST3_PLUGIN_EXAMPLES OFF CACHE BOOL "" FORCE)
    set(SMTG_ENABLE_VSTGUI_SUPPORT OFF CACHE BOOL "" FORCE)
    set(SMTG_CREATE_PLUGIN_LINK OFF CACHE BOOL "" FORCE)      # never link dev builds into ~/Library
    set(SMTG_BUILD_UNIVERSAL_BINARY OFF CACHE BOOL "" FORCE)
    set(SMTG_ENABLE_AUV2_BUILDS OFF CACHE BOOL "" FORCE)
    add_subdirectory(${VST3_SDK_ROOT} ${PROJECT_BINARY_DIR}/vst3sdk)
    smtg_enable_vst3_sdk()

    smtg_add_vst3plugin(ReferenceProbe
        vst3/probe_vst3.h
        vst3/probe_vst3.cpp
        vst3/entry_reference.cpp
    )
    target_include_directories(ReferenceProbe PRIVATE vst3)
    target_compile_definitions(ReferenceProbe PRIVATE
        PROBE_VERSION="${PROBE_VERSION}"
        PROBE_ID_AUTOMATABLE=$<BOOL:${PROBE_ID_AUTOMATABLE}>)
    target_link_libraries(ReferenceProbe PRIVATE sdk reference_engine liveprobe_core)
    if(APPLE)
        smtg_target_set_bundle(ReferenceProbe
            BUNDLE_IDENTIFIER org.livecli.reference-probe
            COMPANY_NAME "LiveCLI")
        # the SDK fills Info.plist only for Xcode builds: configure our own
        set(PROBE_BUNDLE_EXECUTABLE ReferenceProbe)
        set(PROBE_BUNDLE_ID org.livecli.reference-probe)
        set(PROBE_BUNDLE_NAME "Reference Probe")
        set(PROBE_COPYRIGHT "MIT License, LiveCLI contributors")
        configure_file(vst3/mac/Info.plist.in ${PROJECT_BINARY_DIR}/ReferenceProbe-Info.plist @ONLY)
        set_target_properties(ReferenceProbe PROPERTIES
            MACOSX_BUNDLE_INFO_PLIST ${PROJECT_BINARY_DIR}/ReferenceProbe-Info.plist)
        # export only the three VST3 entry points
        smtg_target_set_exported_symbols(ReferenceProbe ${VST3_SDK_ROOT}/public.sdk/source/main/macexport.exp)
    endif()

    # real-time test host (loads any probe bundle)
    add_executable(probe_vst3host
        tests/vst3host.cpp
        ${VST3_SDK_ROOT}/public.sdk/source/vst/hosting/plugprovider.cpp
        ${VST3_SDK_ROOT}/public.sdk/source/vst/hosting/module_mac.mm)
    set_source_files_properties(${VST3_SDK_ROOT}/public.sdk/source/vst/hosting/module_mac.mm
        PROPERTIES COMPILE_FLAGS "-fobjc-arc")
    target_link_libraries(probe_vst3host PRIVATE sdk_hosting "-framework Cocoa")
endif()
