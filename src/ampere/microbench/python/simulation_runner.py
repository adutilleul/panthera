#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Run amperesys on a generated trace and parse its metrics.

This is the v1 subprocess runner for the Python sidecar. Step 3.2 replaces it
with an in-process `SimulationRunner` when the C++ server is available.
"""

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

COMPLETED = re.compile(r"Panthera completed\s*:\s*([\d,]+) cycles,\s*([\d,]+) instructions")
MEASURED = re.compile(r"Simulator measurement\s*:\s*([\d,]+) cycles,\s*([\d,]+) instructions,\s*([\d.]+) IPC")


class SimulationError(RuntimeError):
    pass


@dataclass
class SimulationResult:
    cycles: int
    instructions: int
    ipc: float
    wall_ns: int
    log: str

    def summary(self) -> str:
        return f"cycles={self.cycles} sim_instructions={self.instructions} ipc={self.ipc:.3f}"


def parse_metrics(output: str) -> tuple[int, int, float]:
    measured = MEASURED.search(output)
    if measured:
        return (
            int(measured.group(1).replace(",", "")),
            int(measured.group(2).replace(",", "")),
            float(measured.group(3)),
        )
    completed = COMPLETED.search(output)
    if completed:
        return int(completed.group(1).replace(",", "")), int(completed.group(2).replace(",", "")), 0.0
    raise SimulationError("no Panthera metrics in amperesys output")


class SimulationRunner:
    def __init__(
        self,
        amperesys: str | os.PathLike | None = None,
        config: str | os.PathLike | None = None,
        timeout_s: float = 600.0,
    ):
        root = Path(__file__).resolve().parents[4]
        amperesys_name = str(amperesys or os.environ.get("PANTHERA_AMPERESYS") or root / "install/bin/amperesys")
        resolved = shutil.which(amperesys_name) or (amperesys_name if Path(amperesys_name).exists() else None)
        self.amperesys = Path(resolved) if resolved else Path(amperesys_name)
        self.config = Path(config or os.environ.get("PANTHERA_CONFIG") or root / "install/config/model/sys/siryn.cfg")
        self.timeout_s = timeout_s

        if not self.amperesys.exists():
            raise SimulationError(f"amperesys not found: {self.amperesys}. Set PANTHERA_AMPERESYS or pass --amperesys.")
        if not self.config.exists():
            raise SimulationError(f"config not found: {self.config}. Set PANTHERA_CONFIG or pass --config.")

    def run(
        self,
        trace_path: str | os.PathLike,
        max_instructions: int,
        timeout_s: float | None = None,
    ) -> SimulationResult:
        trace = Path(trace_path)
        if not trace.exists():
            raise SimulationError(f"trace not found: {trace}")
        if max_instructions <= 0:
            raise SimulationError("max_instructions must be positive")

        cmd = [
            str(self.amperesys),
            "-cfg",
            str(self.config),
            "-sys.soc_0.core_0.instruction_stream_name",
            str(trace),
            "-sys.soc_0.core_0.gpc.max_instructions",
            str(max_instructions),
            "-max_time",
            "100ms",
        ]
        effective_timeout = min(self.timeout_s, timeout_s) if timeout_s else self.timeout_s
        started = time.perf_counter_ns()
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as error:
            raise SimulationError(f"amperesys timed out after {effective_timeout:.1f}s") from error
        except OSError as error:
            raise SimulationError(f"failed to run amperesys: {error}") from error
        wall_ns = time.perf_counter_ns() - started
        output = result.stdout + result.stderr

        if result.returncode != 0:
            tail = output.strip().splitlines()[-1] if output.strip() else ""
            raise SimulationError(f"amperesys failed (exit {result.returncode}): {tail[:300]}")

        cycles, instructions, ipc = parse_metrics(output)
        return SimulationResult(
            cycles=cycles,
            instructions=instructions,
            ipc=ipc,
            wall_ns=wall_ns,
            log=output,
        )
