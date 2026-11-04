//
// Copyright (c) 2026 Ampere Computing. All rights reserved.
// SPDX-License-Identifier: BSD-3-Clause
//
// Host-environment shim: some environments ship libsystemc built with a
// different SC_CPLUSPLUS than this project uses, so SystemC's link-time ABI
// version stub has a different mangled name. This defines the stub this build
// expects. It only satisfies the link-time tripwire and must not be used for a
// normal, matched build.
//
#include <sysc/kernel/sc_ver.h>

namespace sc_core {
SC_API_VERSION_STRING::SC_API_VERSION_STRING(sc_writer_policy, bool) {}
} // namespace sc_core

/*
 * vim: sw=4:ts=4:et:si:cindent:cino=\:0,(0,g0,t0:
 */
