#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Run raw AArch64 words through user-mode QEMU and the tracing plugin.

The microbenchmark server receives machine-code bytes. This module wraps them
in a tiny static AArch64 ELF, runs it under the tracing-QEMU `qemu-aarch64`
with `libinstruction_tracer.so`, and returns the generated `.is.xz` plus its
metadata. It does not run the simulator; that is the runner's job.
"""

import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

# The harness places the user code at this address. The `_start` prologue is
# below it and the code is followed by a self-loop.
CODE_BASE = 0x400000
CODE_ENTRY = 0x400100


class TraceError(RuntimeError):
    pass


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
    log: str

    def summary(self) -> str:
        return (
            f"trace={self.path} instructions={self.instructions} "
            f"(measurement={self.measurement}, cooldown={self.cooldown}) "
            f"memops={self.memops} memory={self.memory_events} sysregs={self.sysreg_events}"
        )


def _harness_assembly(code: bytes) -> str:
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
    lines += [".section .bss", ".align 16", "    .space 0x10000", "stack_top:", ""]
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
    ):
        self.qemu = Path(qemu or os.environ.get("PANTHERA_QEMU_AARCH64", "qemu-aarch64"))
        self.plugin = Path(plugin or os.environ.get("PANTHERA_TRACER_PLUGIN", ""))
        self.is_info = Path(is_info) if is_info else None
        self.workdir = Path(workdir) if workdir else Path(tempfile.mkdtemp(prefix="panthera-trace-"))
        self.timeout_s = timeout_s
        self.workdir.mkdir(parents=True, exist_ok=True)

        for name, path in (("qemu", self.qemu), ("plugin", self.plugin)):
            if not str(path) or not path.exists():
                raise TraceError(
                    f"{name} not found: {path!s}. Set PANTHERA_QEMU_AARCH64/"
                    "PANTHERA_TRACER_PLUGIN or pass --qemu/--plugin."
                )

    def build_harness(self, code: bytes, name: str = "bench") -> Path:
        asm = self.workdir / f"{name}.S"
        elf = self.workdir / f"{name}.elf"
        asm.write_text(_harness_assembly(code))
        cmd = [
            "aarch64-linux-gnu-gcc",
            "-nostdlib",
            "-static",
            "-Wl,-Ttext=0x400000",
            "-Wl,-e,_start",
            "-Wl,--build-id=none",
            "-o",
            str(elf),
            str(asm),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            raise TraceError(f"harness assembly failed: {result.stderr.strip()}")
        return elf

    def run(self, code: bytes, max_instructions: int, name: str = "bench") -> TraceResult:
        if not code:
            raise TraceError("code is empty")
        if max_instructions == 0:
            raise TraceError("max_instructions must be non-zero")

        elf = self.build_harness(code, name)
        trace_base = self.workdir / name
        trace_path = self.workdir / f"{name}_0.is.xz"
        log_path = self.workdir / f"{name}.plugin.log"
        for stale in (trace_path, self.workdir / f"{name}_0.is.xz.tmp", log_path):
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
                "cooldown-instructions=100",
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

        started = time.perf_counter_ns()
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.monotonic() + self.timeout_s
        while time.monotonic() < deadline:
            if trace_path.exists() and trace_path.stat().st_size > 0:
                # Give the writer a moment to finish the final rename/flush.
                time.sleep(0.05)
                break
            if process.poll() is not None:
                break
            time.sleep(0.02)

        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        _, stderr = process.communicate()
        generation_ns = time.perf_counter_ns() - started

        if not trace_path.exists() or trace_path.stat().st_size == 0:
            log = log_path.read_text(errors="replace") if log_path.exists() else ""
            raise TraceError(
                "QEMU produced no trace. "
                f"exit={process.returncode} stderr={stderr.strip()[:300]} "
                f"log={log.strip()[-300:]}"
            )

        info = {}
        if self.is_info and self.is_info.exists():
            info_result = subprocess.run(
                [str(self.is_info), str(trace_path)], capture_output=True, text=True, timeout=60
            )
            if info_result.returncode == 0:
                info = _parse_is_info(info_result.stdout + info_result.stderr)

        log = log_path.read_text(errors="replace") if log_path.exists() else ""
        return TraceResult(
            path=trace_path,
            instructions=info.get("instructions", 0),
            warmup=info.get("warmup", 0),
            measurement=info.get("measurement", 0),
            cooldown=info.get("cooldown", 0),
            memops=info.get("memops", 0),
            memory_events=info.get("memory_events", 0),
            sysreg_events=info.get("sysreg_events", 0),
            generation_ns=generation_ns,
            log=log,
        )
