#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Unit and optional integration tests for the user-mode trace source."""

import hashlib
import os
import unittest

from qemu_trace_source import (
    CODE_BASE,
    CODE_ENTRY,
    QemuTraceSource,
    TraceError,
    _harness_assembly,
    _parse_is_info,
)


class HarnessTest(unittest.TestCase):
    def test_assembly_contains_code_and_stack(self):
        code = (0xD2800C80).to_bytes(4, "little")
        assembly = _harness_assembly(code)
        self.assertIn("code_entry:", assembly)
        self.assertIn(".word 0xd2800c80", assembly)
        self.assertIn("stack_top:", assembly)
        self.assertIn("b .", assembly)
        self.assertIn(f".org {CODE_ENTRY - CODE_BASE:#x}", assembly)

    def test_rejects_misaligned_code(self):
        with self.assertRaises(TraceError):
            _harness_assembly(b"\x00")


class IsInfoTest(unittest.TestCase):
    def test_parse(self):
        text = """stream version = 6
total instruction count = 1101
  (0 warmup, 1000 measurement, and 101 cooldown)
memop count = 112
memory count = 54
sysreg count = 0"""
        info = _parse_is_info(text)
        self.assertEqual(info["instructions"], 1101)
        self.assertEqual(info["measurement"], 1000)
        self.assertEqual(info["cooldown"], 101)
        self.assertEqual(info["memops"], 112)
        self.assertEqual(info["memory_events"], 54)


class MissingToolsTest(unittest.TestCase):
    def test_missing_paths(self):
        with self.assertRaises(TraceError):
            QemuTraceSource(qemu="/does/not/exist", plugin="/does/not/exist", is_info="/does/not/exist")


@unittest.skipUnless(
    os.environ.get("PANTHERA_QEMU_AARCH64")
    and os.environ.get("PANTHERA_TRACER_PLUGIN")
    and os.environ.get("PANTHERA_IS_INFO"),
    "set PANTHERA_QEMU_AARCH64, PANTHERA_TRACER_PLUGIN, PANTHERA_IS_INFO for the integration test",
)
class IntegrationTest(unittest.TestCase):
    def test_one_instruction_trace(self):
        source = QemuTraceSource(timeout_s=60)
        code = (0xD2800C80).to_bytes(4, "little")
        result = source.run(code, 1, name="unittest")
        self.assertGreaterEqual(result.measurement, 1)
        self.assertEqual(result.code_sha256, hashlib.sha256(code).hexdigest())


if __name__ == "__main__":
    unittest.main()
