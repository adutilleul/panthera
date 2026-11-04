//
// Copyright (c) 2026 Ampere Computing. All rights reserved.
// SPDX-License-Identifier: BSD-3-Clause
//
// Step 0.2 spike: run a hand-assembled AArch64 loop from raw bytes through
// DriverCode, the perfect frontend, and SocForCorePV.
//
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <vector>

#include <fmt/format.h>
#include <gtest/gtest.h>
#include <unistd.h>

#include "ampere/archx/driver_code.h"
#include "ampere/sc/testing/test_module.h"
#include "ampere/sys/sys.h"

// Host-environment shim: the system libsystemc is built with a different
// SC_CPLUSPLUS than this project's add_cpp_test default, so SystemC's
// link-time ABI version stub has a different mangled name. This mirrors
// core/test/test_fe_footprint.cc and is only for this spike build.
#if defined(DRIVER_CODE_ABI_SHIM)
namespace sc_core {
SC_API_VERSION_STRING::SC_API_VERSION_STRING(sc_writer_policy, bool) {}
} // namespace sc_core
#endif

namespace ampere {
namespace archx {

using namespace ampere::sc;

class DriverCodeSpikeTest : public IntegrationTest
{
protected:
    void SetUp() override
    {
        // The direct-driver tests do not go through Core::construct(), which is
        // what normally installs the decoder set.
        Instruction::set_decoders({"furiosa"});

        // mov x0,#100; mov x1,#0; add x1,x1,#1; subs x0,x0,#1; b.ne .-8; b .
        words_ = {0xd2800c80, 0xd2800001, 0x91000421, 0xf1000400, 0x54ffffc1, 0x14000000};

        path_ = std::filesystem::temp_directory_path()
                / fmt::format("panthera_driver_code_spike_{}.bin", static_cast<long>(::getpid()));

        std::ofstream out(path_, std::ios::binary);
        ASSERT_TRUE(out) << "cannot create " << path_;
        out.write(reinterpret_cast<const char*>(words_.data()),
                  static_cast<std::streamsize>(words_.size() * sizeof(std::uint32_t)));
        ASSERT_TRUE(out.good());
    }

    void TearDown() override
    {
        std::error_code ec;
        std::filesystem::remove(path_, ec);
    }

    std::vector<std::uint32_t> words_;
    std::filesystem::path path_;
};

TEST_F(DriverCodeSpikeTest, DirectLoopReachesHalt)
{
    DriverCode driver(path_.string());
    const ProgramCounter halt{0x400000 + 5 * 4};

    bool reached_halt = false;
    for (int i = 0; i < 400; ++i) {
        const auto pc = driver.next_pc();
        auto instruction = driver.fetch(pc);
        driver.commit(instruction);
        if (pc == halt) {
            reached_halt = true;
            break;
        }
    }

    EXPECT_TRUE(reached_halt);
    EXPECT_EQ(driver.read_gpr(LReg::L1), 100u);
    EXPECT_EQ(driver.read_gpr(LReg::L0), 0u);
}

TEST_F(DriverCodeSpikeTest, WrongPathFetchUsesNegativeInum)
{
    DriverCode driver(path_.string());
    auto first = driver.fetch(driver.next_pc());
    driver.commit(first);

    // Valid code, but not the expected next PC.
    auto wrong = driver.fetch(ProgramCounter{0x400000});
    EXPECT_TRUE(wrong->wrong_path());
}

TEST_F(DriverCodeSpikeTest, RunsLoopFromRawBytes)
{
    auto* env = get_environment();
    env->parameters.load(ampere::sc::TestModule::testconfig("sys/kestrel-user.cfg"));
    env->parameters.add_setting(
        "sys.soc_0.core_0.instruction_stream_name", "code:" + path_.string(), "driver code spike");
    env->parameters.add_setting("sys.soc_0.core_0.perfect_frontend", true, "driver code spike");
    env->parameters.add_setting("sys.soc_0.core_0.speculative_memory_image", false, "driver code spike");
    env->parameters.add_setting("sys.soc_0.core_0.gpc.max_instructions", 200, "driver code spike");
    env->parameters.add_setting("sys.soc_0.core_0.gpc.warmup_instructions", 0, "driver code spike");
    // The kestrel-user config does not load a *-perfect-frontend.cfg, so the
    // PFE's FIFO/credit settings must be supplied here.
    env->parameters.add_setting("sys.soc_0.core_0.pfe.instruction_fifo.size", 32, "driver code spike");
    env->parameters.add_setting("sys.soc_0.core_0.pfe.branch_commit_limit", 2, "driver code spike");
    env->parameters.add_setting("sys.soc_0.core_0.feq_credit_return_fifo.bandwidth", 8, "driver code spike");
    env->parameters.add_setting("sys.soc_0.core_0.pfe_out_to_idr_in_fifo.bandwidth", 256, "driver code spike");
    env->parameters.add_setting("sys.soc_0.core_0.pfe_out_to_idr_in_fifo.latency", 0, "driver code spike");
    env->parameters.add_setting("sys.soc_0.core_0.instruction_fifo_credit_return.bandwidth", 256, "driver code spike");

    Sys sys("sys", clock, env);

    // Bypass IntegrationTest::run_for(): the perfect frontend intentionally
    // does not instantiate the BPU/ICF, so their settings from the core config
    // are unused. The spike only needs the commit count.
    env->event_stream->enable();
    sc_core::sc_start(sc_core::sc_time{10, sc_core::SC_US});
    if (sc_core::sc_get_status() != sc_core::SC_STOPPED)
        sc_core::sc_stop();

    const auto retired = event_count("sys.soc_0.core_0.gpc.instruction_commit");
    EXPECT_GE(retired, 100u);
    EXPECT_LT(retired, 1000u);
    EXPECT_GT(simulated_ipc(), 0.0f);
}

} // namespace archx
} // namespace ampere

/*
 * vim: sw=4:ts=4:et:si:cindent:cino=\:0,(0,g0,t0:
 */
