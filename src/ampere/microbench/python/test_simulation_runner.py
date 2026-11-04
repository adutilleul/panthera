#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Unit and optional integration tests for the amperesys subprocess runner."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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


class FakeProcess:
    def __init__(self, stdout="", stderr="", returncode=0, timeout=False):
        self.pid = 12345
        self._stdout = stdout
        self._stderr = stderr
        self._timeout = timeout
        self.returncode = returncode

    def communicate(self, timeout=None):
        if self._timeout:
            self._timeout = False
            raise subprocess.TimeoutExpired(cmd="amperesys", timeout=timeout)
        return self._stdout, self._stderr

    def kill(self):
        self.returncode = -9


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        directory = Path(self.temp.name)
        self.config = directory / "test.cfg"
        self.config.write_text("")
        self.trace = directory / "bench_0.is.xz"
        self.trace.write_bytes(b"trace")
        self.runner = SimulationRunner(amperesys="/bin/true", config=self.config)

    def tearDown(self):
        self.temp.cleanup()

    def test_command_and_metrics(self):
        process = FakeProcess(stdout=" Simulator measurement : 10 cycles, 5 instructions, 2.000 IPC")
        with mock.patch("simulation_runner.subprocess.Popen", return_value=process) as popen:
            result = self.runner.run(self.trace, 1000)
        command = popen.call_args.args[0]
        self.assertIn("1000", command)
        self.assertIn("-max_time", command)
        self.assertEqual((result.cycles, result.instructions, result.ipc), (10, 5, 2.0))

    def test_nonzero_exit(self):
        process = FakeProcess(stderr="boom", returncode=1)
        with mock.patch("simulation_runner.subprocess.Popen", return_value=process):
            with self.assertRaises(SimulationError):
                self.runner.run(self.trace, 1)

    def test_timeout(self):
        process = FakeProcess(timeout=True)
        with mock.patch("simulation_runner.subprocess.Popen", return_value=process):
            with self.assertRaises(TimeoutError):
                self.runner.run(self.trace, 1)

    def test_missing_trace(self):
        with self.assertRaises(SimulationError):
            self.runner.run("/does/not/exist.is.xz", 1)


class MissingToolsTest(unittest.TestCase):
    def test_missing_binary(self):
        with self.assertRaises(SimulationError):
            SimulationRunner(amperesys="/does/not/exist", config="/does/not/exist")

    def test_missing_config(self):
        with self.assertRaises(SimulationError):
            SimulationRunner(amperesys="/bin/true", config="/does/not/exist")


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
