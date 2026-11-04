//
// Copyright (c) 2026 Ampere Computing. All rights reserved.
// SPDX-License-Identifier: BSD-3-Clause
//
#include "ampere/archx/driver_code.h"

#include <cstring>
#include <fstream>
#include <iterator>
#include <stdexcept>

#include <fmt/format.h>

#include "ampere/archx/emulate.h"

namespace ampere::archx {

namespace {

// Step 0.2 spike: no page tables, identity VA->PA, fixed load address.
class CodeInstructionIS : public InstructionIS
{
public:
    CodeInstructionIS(ProgramCounter pc,
                      std::uint32_t opcode,
                      inum_t instruction_id,
                      inum_t prior_instruction_id,
                      ArchState state,
                      PhysAddr pc_pa)
        : InstructionIS(pc, opcode, instruction_id, prior_instruction_id, std::move(state)), pc_pa_{pc_pa}
    {
    }

    PhysAddr program_counter_pa() const override { return pc_pa_; }

    // Identity mapping for the spike. Step 2.1 adds a real flat page table.
    PhysAddr physical_address(unsigned n = 0) const override { return PhysAddr{virtual_address(n).value()}; }

    bool memop_predicated_off(unsigned) const override { return false; }

    // Step 0.2 does not exercise loads/stores. The stream-backed
    // implementation reads this from is::Instruction::memop(n).bytes();
    // deriving it from the encoding is a Step 2.4 task.
    std::uint32_t memop_bytes(unsigned) const override
    {
        throw std::logic_error("DriverCode: memop_bytes is not implemented yet");
    }

private:
    PhysAddr pc_pa_;
};

} // namespace

DriverCode::DriverCode(std::string filename)
{
    std::ifstream file(filename, std::ios::binary);
    if (!file)
        throw std::runtime_error(fmt::format("DriverCode: cannot open {}", filename));

    std::vector<std::uint8_t> bytes{std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>()};
    if (bytes.size() % sizeof(std::uint32_t) != 0)
        throw std::runtime_error(fmt::format("DriverCode: {} is not a multiple of 4 bytes", filename));

    code_.resize(bytes.size() / sizeof(std::uint32_t));
    std::memcpy(code_.data(), bytes.data(), bytes.size());

    next_pc_ = ProgramCounter{base_va_};
}

std::uint32_t DriverCode::read_word(VirtAddr va) const
{
    if (va.value() < base_va_)
        throw std::runtime_error(fmt::format("DriverCode: fetch below code base at {:#x}", va.value()));
    const auto offset = va.value() - base_va_;
    if (offset % sizeof(std::uint32_t) != 0)
        throw std::runtime_error(fmt::format("DriverCode: unaligned fetch at {:#x}", va.value()));
    const auto index = offset / sizeof(std::uint32_t);
    if (index >= code_.size())
        throw std::runtime_error(fmt::format("DriverCode: fetch past end of code image at {:#x}", va.value()));
    return code_[index];
}

ProgramCounter DriverCode::next_pc(bool* is_taken_branch)
{
    if (is_taken_branch)
        *is_taken_branch = last_taken_;
    return next_pc_;
}

std::shared_ptr<Instruction> DriverCode::fetch(ProgramCounter pc)
{
    return make_instruction(pc, pc == next_pc_);
}

std::shared_ptr<InstructionIS> DriverCode::make_instruction(ProgramCounter pc, bool advance)
{
    const auto opcode = read_word(VirtAddr{pc.value()});
    const inum_t id{advance ? next_id_++ : wrong_path_id_--};
    const inum_t prior{last_fetched_id_};

    auto instruction = std::make_shared<CodeInstructionIS>(pc, opcode, id, prior, fetch_state_, PhysAddr{pc.value()});

    emulate::emulate_correct_path(instruction.get());

    if (!instruction->emulated())
        throw std::runtime_error(fmt::format("DriverCode: unemulated opcode {:#010x} at pc {:#x}", opcode, pc.value()));

    if (advance) {
        fetch_state_ = instruction->arch_state_output;
        next_pc_ = instruction->next_program_counter();
        last_taken_ = instruction->is_taken_branch();
        last_fetched_id_ = id.id();
        history_[id.id()] = instruction;
    }

    return instruction;
}

void DriverCode::commit(std::shared_ptr<Instruction> instruction)
{
    if (instruction->wrong_path())
        return;

    if (history_.empty())
        throw std::runtime_error("DriverCode: commit called with no outstanding instruction");

    const auto first = history_.begin();
    if (instruction->inum().id() != first->first)
        throw std::runtime_error(
            fmt::format("DriverCode: commit order error, expected {} got {}", first->first, instruction->inum().id()));

    auto instruction_is = std::dynamic_pointer_cast<InstructionIS>(instruction);
    if (instruction_is && instruction_is->emulated())
        arch_state_ = instruction_is->arch_state_output;
    else
        arch_state_ = instruction->state();

    last_committed_id_ = static_cast<uint64_t>(instruction->inum().id());
    history_.erase(first);
}

void DriverCode::recover(inum_t id)
{
    auto it = history_.find(id.id());
    if (it == history_.end())
        throw std::runtime_error(fmt::format("DriverCode: recover with unknown id {}", id.id()));

    fetch_state_ = it->second->arch_state_output;
    next_pc_ = it->second->next_program_counter();
    last_fetched_id_ = id.id();

    for (auto i = history_.upper_bound(id.id()); i != history_.end();)
        i = history_.erase(i);
}

uint64_t DriverCode::read_memory(PhysAddr addr) const
{
    return arch_state_.read(addr);
}

uint64_t DriverCode::read_sysreg(SysReg sysreg) const
{
    return arch_state_.read(sysreg);
}

EL DriverCode::exception_level() const
{
    return arch_state_.exception_level();
}

uint16_t DriverCode::asid() const
{
    // Spike limitation: no TCR/TTBR-based ASID extraction yet.
    return 0;
}

uint64_t DriverCode::committed_id() const
{
    return last_committed_id_;
}

} // namespace ampere::archx

/*
 * vim: sw=4:ts=4:et:si:cindent:cino=\:0,(0,g0,t0:
 */
