#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Unit and optional integration tests for the user-mode trace source."""

import hashlib
import os
import struct
import tempfile
import unittest
from pathlib import Path

from qemu_trace_source import (
    CODE_BASE,
    CODE_ENTRY,
    DATA_BASE,
    FLAT_TABLE_PA,
    QemuTraceSource,
    TraceError,
    _harness_assembly,
    _parse_is_info,
    validate_data_sections,
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


class FlatPageTableTest(unittest.TestCase):
    def test_table(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "bench_0.is.xz"
            trace.write_bytes(b"trace")
            mem = QemuTraceSource._write_flat_page_table(trace)
            data = mem.read_bytes()
            self.assertEqual(len(data), 12 + 4096)
            address, size = struct.unpack("<QI", data[:12])
            self.assertEqual(address, FLAT_TABLE_PA)
            self.assertEqual(size, 4096)
            self.assertEqual(struct.unpack("<Q", data[12:20])[0], 0x401)


class DataSectionsTest(unittest.TestCase):
    def test_validation(self):
        with self.assertRaises(TraceError):
            validate_data_sections([(DATA_BASE - 1, b"x")])
        with self.assertRaises(TraceError):
            validate_data_sections([(DATA_BASE, b"x"), (DATA_BASE, b"y")])
        self.assertEqual(validate_data_sections([(DATA_BASE + 16, b"y"), (DATA_BASE, b"x")])[0][0], DATA_BASE)

    def test_memory_image_includes_data(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "bench_0.is.xz"
            trace.write_bytes(b"trace")
            payload = (41).to_bytes(8, "little")
            mem = QemuTraceSource._write_flat_page_table(trace, [(DATA_BASE, payload)])
            data = mem.read_bytes()
            record = 12 + 4096
            address, size = struct.unpack("<QI", data[record : record + 12])
            self.assertEqual(address, DATA_BASE)
            self.assertEqual(size, 8)
            self.assertEqual(data[record + 12 : record + 20], payload)

    def test_harness_includes_data_section(self):
        assembly = _harness_assembly((0xD2800C80).to_bytes(4, "little"), [Path("data.bin")])
        self.assertIn('.section .data_0,"aw",@progbits', assembly)
        self.assertIn('.incbin "data.bin"', assembly)


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
        self.assertTrue(result.mem_path.exists())
        self.assertEqual(result.mem_path.stat().st_size, 12 + 4096)

    def test_code_too_large_for_flat_layout(self):
        source = QemuTraceSource(timeout_s=1)
        code = b"\x00" * (FLAT_TABLE_PA - CODE_ENTRY)
        with self.assertRaises(TraceError):
            source.run(code, 1)

    def test_trace_with_data(self):
        source = QemuTraceSource(timeout_s=60)
        # movz x1, #0xb0, lsl #16; loop: ldr x0,[x1]; add; str; b loop
        words = [0xD2A01601, 0xF9400020, 0x91000400, 0xF9000020, 0x17FFFFFD]
        code = b"".join(word.to_bytes(4, "little") for word in words)
        payload = (41).to_bytes(8, "little")
        result = source.run(code, 100, data_sections=[(DATA_BASE, payload)], name="data")
        self.assertGreaterEqual(result.measurement, 100)
        self.assertGreater(result.memops, 0)
        self.assertGreater(result.mem_path.stat().st_size, 12 + 4096)


if __name__ == "__main__":
    unittest.main()
