#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Unit and optional integration tests for the amperesys subprocess runner."""

import os
import unittest

from qemu_trace_source import QemuTraceSource
from simulation_runner import SimulationError, SimulationRunner, parse_metrics

_SIM_ENV = (
    "PANTHERA_QEMU_AARCH64",
    "PANTHERA_TRACER_PLUGIN",
    "PANTHERA_IS_INFO",
    "PANTHERA_AMPERESYS",
    "PANTHERA_CONFIG",
)


class ParseMetricsTest(unittest.TestCase):
    def test_measurement(self):
        text = " Simulator measurement : 845 cycles, 1,000 instructions, 1.183 IPC, 28 kHz"
        self.assertEqual(parse_metrics(text), (845, 1000, 1.183))

    def test_completed_fallback(self):
        text = "Panthera completed : 1,583 cycles, 1,001 instructions total, in 0 cpu sec"
        self.assertEqual(parse_metrics(text), (1583, 1001, 0.0))

    def test_missing_metrics(self):
        with self.assertRaises(SimulationError):
            parse_metrics("no metrics here")


class MissingToolsTest(unittest.TestCase):
    def test_missing_paths(self):
        with self.assertRaises(SimulationError):
            SimulationRunner(amperesys="/does/not/exist", config="/does/not/exist")


@unittest.skipUnless(all(os.environ.get(name) for name in _SIM_ENV), "set all PANTHERA_* runner env vars")
class IntegrationTest(unittest.TestCase):
    def test_trace_and_simulate(self):
        source = QemuTraceSource(timeout_s=60)
        runner = SimulationRunner()
        code = (0xD2800C80).to_bytes(4, "little")
        trace = source.run(code, 1, name="runner_unittest")
        result = runner.run(trace.path, 1)
        self.assertGreater(result.cycles, 0)
        self.assertGreaterEqual(result.instructions, 1)


if __name__ == "__main__":
    unittest.main()
