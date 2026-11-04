//
// Copyright (c) 2026 Ampere Computing. All rights reserved.
// SPDX-License-Identifier: BSD-3-Clause
//
// Step 0.2 spike: a minimal raw-code Driver.
//
// This is deliberately narrow: it executes a contiguous AArch64 code image at
// a fixed base address, assumes an identity VA->PA mapping, and is intended to
// run behind the perfect frontend only. It exists to discover which
// InstructionIS/emulate assumptions break when the functional state comes from
// emulation instead of an instruction-stream trace.
//
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "ampere/archx/arch_state.h"
#include "ampere/archx/driver.h"
#include "ampere/archx/instruction_is.h"

namespace ampere::archx {

class DriverCode : public Driver
{
public:
    // Loads a raw little-endian AArch64 image at a fixed base address.
    explicit DriverCode(std::string filename);

    ProgramCounter next_pc(bool* is_taken_branch = nullptr) override;
    std::shared_ptr<Instruction> fetch(ProgramCounter pc) override;
    void commit(std::shared_ptr<Instruction> instruction) override;
    void recover(inum_t id) override;

    uint64_t read_memory(PhysAddr addr) const override;
    uint64_t read_sysreg(SysReg sysreg) const override;
    EL exception_level() const override;
    uint16_t asid() const override;
    uint64_t committed_id() const override;

    // Spike-only introspection for the direct driver test.
    ProgramCounter pc() const { return next_pc_; }
    uint64_t read_gpr(LReg reg) const { return arch_state_.read(reg); }

private:
    std::shared_ptr<InstructionIS> make_instruction(ProgramCounter pc, bool advance);
    uint32_t read_word(VirtAddr va) const;

    std::vector<uint32_t> code_;
    uint64_t base_va_{0x400000};
    ProgramCounter next_pc_;
    bool last_taken_{false};

    ArchState arch_state_;
    ArchState fetch_state_;

    int64_t next_id_{1};
    int64_t wrong_path_id_{-1};
    int64_t last_fetched_id_{0};
    uint64_t last_committed_id_{0};
    std::map<int64_t, std::shared_ptr<InstructionIS>> history_;
};

} // namespace ampere::archx

/*
 * vim: sw=4:ts=4:et:si:cindent:cino=\:0,(0,g0,t0:
 */
