#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Run raw AArch64 words through user-mode QEMU and the tracing plugin.

The microbenchmark server receives machine-code bytes. This module wraps them
in a tiny static AArch64 ELF, runs it under the tracing-QEMU `qemu-aarch64`
with `libinstruction_tracer.so`, and returns the generated `.is.xz` plus its
metadata. It does not run the simulator; that is the runner's job.
"""

import hashlib
import os
import re
import shutil
import struct
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from process_registry import register_child, unregister_child

# The harness places the user code at this address. The `_start` prologue is
# below it and the code is followed by a self-loop.
CODE_BASE = 0x400000
CODE_ENTRY = 0x400100
# Data sections live above the stack (0x600000) and inside the flat 1 GiB map.
DATA_BASE = 0x700000
DATA_LIMIT = 0x40000000
MAX_DATA_BYTES = 2 * 1024 * 1024

# Flat stage-1 mapping: 4 KiB granule, 39-bit VA, one level-1 1 GiB block at
# VA/PA 0. These values must match the synthetic TCR/TTBR written by the
# plugin's flat mode.
FLAT_TABLE_PA = 0x500000
FLAT_TCR_EL1 = 25 | (2 << 32)  # T0SZ=25, IPS=2


class TraceError(RuntimeError):
    pass


def validate_data_sections(sections) -> list[tuple[int, bytes]]:
    ordered = sorted(((int(address), bytes(data)) for address, data in sections), key=lambda item: item[0])
    total = 0
    previous_end = None
    for address, data in ordered:
        if not data:
            raise TraceError("data section is empty")
        if address < DATA_BASE or address + len(data) > DATA_LIMIT:
            raise TraceError(f"data section at {address:#x} is outside [{DATA_BASE:#x}, {DATA_LIMIT:#x})")
        if previous_end is not None and address < previous_end:
            raise TraceError("data sections overlap")
        previous_end = address + len(data)
        total += len(data)
    if total > MAX_DATA_BYTES:
        raise TraceError(f"data sections exceed {MAX_DATA_BYTES} bytes")
    return ordered


@dataclass
class TraceResult:
    path: Path
    instructions: int
    warmup: int
    measurement: int
    cooldown: int
    memops: int
    memory_events: int
    sysreg_events: int
    generation_ns: int
    code_sha256: str
    mem_path: Path | None
    log: str

    def summary(self) -> str:
        return (
            f"trace={self.path} code={self.code_sha256[:16]} "
            f"instructions={self.instructions} "
            f"(measurement={self.measurement}, cooldown={self.cooldown}) "
            f"memops={self.memops} memory={self.memory_events} sysregs={self.sysreg_events} "
            f"mem={self.mem_path}"
        )


def _harness_assembly(code: bytes, data_paths: list[Path] = ()) -> str:
    if len(code) % 4 != 0:
        raise TraceError("code length must be a multiple of 4 bytes")
    words = [int.from_bytes(code[i : i + 4], "little") for i in range(0, len(code), 4)]
    lines = [
        ".global _start",
        ".text",
        "_start:",
        "    adrp x0, stack_top",
        "    add x0, x0, :lo12:stack_top",
        "    add sp, x0, #0",
    ]
    # Give the benchmark a deterministic register file and stack.
    lines += [f"    mov x{reg}, #0" for reg in range(30)]
    lines += [
        "    bl code_entry",
        "    mov x0, #0",
        "    mov x8, #93",  # exit
        "    svc #0",
        f"    .org {CODE_ENTRY - CODE_BASE:#x}",
        ".global code_entry",
        "code_entry:",
    ]
    lines += [f"    .word 0x{word:08x}" for word in words]
    # If the benchmark falls through, keep it in a safe self-loop rather than
    # executing whatever follows the text section.
    lines += ["    b .", ""]
    lines += ['.section .stack,"aw",@nobits', ".align 16", "    .space 0x10000", "stack_top:", ""]
    for index, path in enumerate(data_paths):
        lines += [f'.section .data_{index},"aw",@progbits', f'    .incbin "{path}"', ""]
    return "\n".join(lines)


def _linker_script(data_sections: list[tuple[int, bytes]]) -> str:
    lines = [
        "ENTRY(_start)",
        "SECTIONS",
        "{",
        "  . = 0x400000;",
        "  .text : { *(.text) }",
        "  . = 0x600000;",
        "  .stack (NOLOAD) : { *(.stack) }",
    ]
    for index, (address, _) in enumerate(data_sections):
        lines += [f"  . = {address:#x};", f"  .data_{index} : {{ *(.data_{index}) }}"]
    lines += ["}", ""]
    return "\n".join(lines)


def _parse_is_info(output: str) -> dict:
    def number(pattern):
        match = re.search(pattern, output, re.MULTILINE)
        return int(match.group(1)) if match else 0

    return {
        "instructions": number(r"total instruction count = (\d+)"),
        "warmup": number(r"\((\d+) warmup,"),
        "measurement": number(r"warmup, (\d+) measurement,"),
        "cooldown": number(r"measurement, and (\d+) cooldown\)"),
        "memops": number(r"memop count = (\d+)"),
        "memory_events": number(r"memory count = (\d+)"),
        "sysreg_events": number(r"sysreg count = (\d+)"),
    }


class QemuTraceSource:
    def __init__(
        self,
        qemu: str | os.PathLike | None = None,
        plugin: str | os.PathLike | None = None,
        is_info: str | os.PathLike | None = None,
        workdir: str | os.PathLike | None = None,
        timeout_s: float = 120.0,
        keep_intermediates: bool = False,
    ):
        qemu_name = str(qemu or os.environ.get("PANTHERA_QEMU_AARCH64", "qemu-aarch64"))
        resolved_qemu = shutil.which(qemu_name) or (qemu_name if Path(qemu_name).exists() else None)
        self.qemu = Path(resolved_qemu) if resolved_qemu else Path(qemu_name)
        self.plugin = Path(plugin or os.environ.get("PANTHERA_TRACER_PLUGIN", ""))

        is_info_name = is_info or os.environ.get("PANTHERA_IS_INFO")
        self.is_info = Path(is_info_name) if is_info_name else None
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="panthera-trace-"))
        self.timeout_s = timeout_s
        self.keep_intermediates = keep_intermediates
        self.workdir.mkdir(parents=True, exist_ok=True)

        for name, path in (("qemu", self.qemu), ("plugin", self.plugin)):
            if not str(path) or not path.exists():
                raise TraceError(
                    f"{name} not found: {path!s}. Set PANTHERA_QEMU_AARCH64/"
                    "PANTHERA_TRACER_PLUGIN or pass --qemu/--plugin."
                )
        if self.is_info is None or not self.is_info.exists():
            raise TraceError("is_info not found; pass --is-info or set PANTHERA_IS_INFO")

    def build_harness(self, code: bytes, data_sections=(), name: str = "bench") -> tuple[Path, Path, list[Path]]:
        ordered = validate_data_sections(data_sections)
        asm = self.workdir / f"{name}.S"
        elf = self.workdir / f"{name}.elf"
        linker = self.workdir / f"{name}.ld"
        data_paths = []
        for index, (_, data) in enumerate(ordered):
            path = self.workdir / f"{name}.data{index}.bin"
            path.write_bytes(data)
            data_paths.append(path)
        asm.write_text(_harness_assembly(code, data_paths))
        linker.write_text(_linker_script(ordered))
        cmd = [
            "aarch64-linux-gnu-gcc",
            "-nostdlib",
            "-static",
            "-no-pie",
            "-fno-pie",
            "-Wl,-e,_start",
            "-Wl,--build-id=none",
            "-T",
            str(linker),
            "-o",
            str(elf),
            str(asm),
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise TraceError(f"harness assembly failed: {error}") from error
        if result.returncode != 0:
            raise TraceError(f"harness assembly failed: {result.stderr.strip()}")
        return asm, elf, [linker, *data_paths]

    def _run_is_info(self, trace_path: Path) -> dict:
        last_error = ""
        for _ in range(2):
            try:
                result = subprocess.run(
                    [str(self.is_info), str(trace_path)], capture_output=True, text=True, timeout=60
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                last_error = str(error)
                break
            if result.returncode == 0:
                return _parse_is_info(result.stdout + result.stderr)
            last_error = (result.stderr or result.stdout).strip()
            time.sleep(0.1)
        raise TraceError(f"is_info rejected the trace: {last_error[:300]}")

    @staticmethod
    def _write_flat_page_table(trace_path: Path, data_sections=()) -> Path:
        """Write the flat page table plus initialized data sections."""
        ordered = validate_data_sections(data_sections)
        mem_path = trace_path.with_name(trace_path.name.replace(".is.xz", ".mem"))
        # Level-1 block descriptor: valid, AF=1, output address 0 (identity).
        entry = 1 | (1 << 10)
        table = struct.pack("<Q", entry) + b"\x00" * (4096 - 8)
        with mem_path.open("wb") as stream:
            stream.write(struct.pack("<QI", FLAT_TABLE_PA, len(table)))
            stream.write(table)
            for address, data in ordered:
                stream.write(struct.pack("<QI", address, len(data)))
                stream.write(data)
        return mem_path

    def run(
        self,
        code: bytes,
        max_instructions: int,
        data_sections=(),
        name: str | None = None,
        timeout_s: float | None = None,
    ) -> TraceResult:
        if not code:
            raise TraceError("code is empty")
        if max_instructions <= 0:
            raise TraceError("max_instructions must be positive")
        data_sections = validate_data_sections(data_sections)

        started = time.perf_counter_ns()
        code_sha = hashlib.sha256(code).hexdigest()
        name = name or f"bench_{uuid.uuid4().hex}"
        asm, elf, intermediates = self.build_harness(code, data_sections, name)
        trace_base = self.workdir / name
        trace_path = self.workdir / f"{name}_0.is.xz"
        tmp_path = self.workdir / f"{name}_0.is.xz.tmp"
        log_path = self.workdir / f"{name}.plugin.log"
        for stale in (trace_path, tmp_path, log_path):
            stale.unlink(missing_ok=True)

        plugin_options = ",".join(
            [
                f"trace-filename-base={trace_base}",
                "trace-is=true",
                "trace-elf=false",
                "max-traces=1",
                f"start-tracing-after-pc=0x{CODE_ENTRY:x}",
                "start-tracing-after-pc-occurrences=1",
                f"stop-tracing-after-measuring-instructions={max_instructions}",
                "cooldown-instructions=1000",
                "allow-missing-sysregs=true",
                "flat-map=true",
            ]
        )
        cmd = [
            str(self.qemu),
            "-plugin",
            f"{self.plugin},{plugin_options}",
            "-d",
            "plugin",
            "-D",
            str(log_path),
            str(elf),
        ]

        effective_timeout = min(self.timeout_s, timeout_s) if timeout_s is not None else self.timeout_s
        deadline = time.monotonic() + effective_timeout
        process = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        register_child(process.pid)
        finalized = False
        try:
            while time.monotonic() < deadline:
                # The tracer removes <trace>.tmp only after the final stream
                # has been copied and finalized, so its absence is the
                # completion signal. The "Stopping tracing" log line is
                # printed before that copy and is not sufficient for large
                # traces.
                if trace_path.exists() and trace_path.stat().st_size > 0 and not tmp_path.exists():
                    finalized = True
                    break
                if process.poll() is not None:
                    break
                time.sleep(0.02)

            if not finalized and trace_path.exists() and trace_path.stat().st_size > 0 and not tmp_path.exists():
                finalized = True

            timed_out = not finalized and process.poll() is None
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        finally:
            unregister_child(process.pid)
        log = log_path.read_text(errors="replace") if log_path.exists() else ""

        if not finalized:
            if timed_out:
                raise TimeoutError(
                    f"QEMU trace did not finalize within {effective_timeout:.1f}s "
                    f"(tmp_present={tmp_path.exists()}, log={log.strip()[-300:]})"
                )
            raise TraceError(
                f"QEMU trace did not finalize within {effective_timeout:.1f}s "
                f"(tmp_present={tmp_path.exists()}, log={log.strip()[-300:]})"
            )
        if not trace_path.exists() or trace_path.stat().st_size == 0:
            raise TraceError(f"QEMU produced no trace within {effective_timeout:.1f}s (log={log.strip()[-300:]})")
        info = self._run_is_info(trace_path)
        if info["measurement"] < max_instructions:
            raise TraceError(f"incomplete trace: measurement={info['measurement']} < requested={max_instructions}")
        mem_path = self._write_flat_page_table(trace_path, data_sections)
        generation_ns = time.perf_counter_ns() - started

        if not self.keep_intermediates:
            for path in [asm, elf, *intermediates]:
                path.unlink(missing_ok=True)

        return TraceResult(
            path=trace_path,
            instructions=info["instructions"],
            warmup=info["warmup"],
            measurement=info["measurement"],
            cooldown=info["cooldown"],
            memops=info["memops"],
            memory_events=info["memory_events"],
            sysreg_events=info["sysreg_events"],
            generation_ns=generation_ns,
            code_sha256=code_sha,
            mem_path=mem_path,
            log=log,
        )
