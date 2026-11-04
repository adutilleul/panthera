#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Unit and integration tests for the trace/result cache."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import microbench_server
from qemu_trace_source import TraceResult
from simulation_runner import SimulationResult
from trace_cache import ResultCache, TraceCache, file_fingerprint


def make_trace(directory: Path, name: str, measurement: int = 1) -> TraceResult:
    trace = directory / f"{name}.is.xz"
    trace.write_bytes(b"trace-" + name.encode())
    mem = directory / f"{name}.mem"
    mem.write_bytes(b"mem-" + name.encode())
    return TraceResult(
        path=trace,
        instructions=measurement,
        warmup=0,
        measurement=measurement,
        cooldown=0,
        memops=0,
        memory_events=0,
        sysreg_events=0,
        generation_ns=1,
        code_sha256="abc",
        mem_path=mem,
        log="",
    )


class KeyTest(unittest.TestCase):
    def test_stable_and_distinct(self):
        cache = ResultCache(tempfile.mkdtemp())
        first = cache.key({"a": 1, "b": 2})
        self.assertEqual(first, cache.key({"b": 2, "a": 1}))
        self.assertNotEqual(first, cache.key({"a": 2, "b": 2}))

    def test_fingerprint(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tool"
            path.write_bytes(b"x")
            first = file_fingerprint(path, hash_contents=True)
            self.assertEqual(first["size"], 1)
            self.assertIn("sha256", first)
            self.assertTrue(file_fingerprint(None)["missing"])


class ResultCacheTest(unittest.TestCase):
    def test_put_get(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(directory)
            result = SimulationResult(cycles=10, instructions=5, ipc=2.0, wall_ns=3, log="")
            key = cache.key({"x": 1})
            cache.put(key, result)
            cached = cache.get(key)
            self.assertIsNotNone(cached)
            self.assertEqual((cached.cycles, cached.instructions, cached.ipc), (10, 5, 2.0))

    def test_corrupt_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(directory)
            key = cache.key({"x": 1})
            (Path(directory) / f"{key}.json").write_text("{not json")
            self.assertIsNone(cache.get(key))
            self.assertFalse((Path(directory) / f"{key}.json").exists())

    def test_eviction(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ResultCache(directory, max_bytes=1)
            first = cache.key({"x": 1})
            second = cache.key({"x": 2})
            cache.put(first, SimulationResult(1, 1, 1.0, 0, ""))
            cache.put(second, SimulationResult(2, 2, 2.0, 0, ""))
            self.assertIsNone(cache.get(first))
            self.assertIsNotNone(cache.get(second))


class TraceCacheTest(unittest.TestCase):
    def test_put_get(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            cache = TraceCache(Path(directory) / "cache")
            trace = make_trace(source, "a", measurement=5)
            key = cache.key({"x": 1})
            cached = cache.put(key, trace)
            self.assertTrue(cached.path.exists())
            self.assertTrue(cached.mem_path.exists())
            self.assertEqual(cached.measurement, 5)
            again = cache.get(key)
            self.assertIsNotNone(again)
            self.assertEqual(again.path.read_bytes(), trace.path.read_bytes())
            self.assertEqual(again.mem_path.read_bytes(), trace.mem_path.read_bytes())

    def test_corrupt_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            cache = TraceCache(Path(directory) / "cache")
            key = cache.key({"x": 1})
            cached = cache.put(key, make_trace(source, "a"))
            (cached.path.parent / "meta.json").write_text("{not json")
            self.assertIsNone(cache.get(key))

    def test_eviction(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            cache = TraceCache(Path(directory) / "cache", max_bytes=1)
            first = cache.key({"x": 1})
            second = cache.key({"x": 2})
            cache.put(first, make_trace(source, "a"))
            cache.put(second, make_trace(source, "b"))
            self.assertIsNone(cache.get(first))
            self.assertIsNotNone(cache.get(second))


class FakeTraceSource:
    qemu = None
    plugin = None
    is_info = None

    def __init__(self, directory: Path):
        self.directory = directory
        self.calls = 0

    def run(self, code, max_instructions, timeout_s=None):
        self.calls += 1
        return make_trace(self.directory, f"trace{self.calls}", max_instructions)


class FakeRunner:
    amperesys = None
    config = None

    def __init__(self):
        self.calls = 0

    def run(self, trace_path, max_instructions, timeout_s=None):
        self.calls += 1
        return SimulationResult(cycles=10, instructions=max_instructions, ipc=1.5, wall_ns=2, log="")


class ServerCacheTest(unittest.TestCase):
    def test_second_request_is_a_result_hit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source_dir = root / "source"
            source_dir.mkdir()
            source = FakeTraceSource(source_dir)
            runner = FakeRunner()
            trace_cache = TraceCache(root / "cache" / "traces")
            result_cache = ResultCache(root / "cache" / "results")
            request = microbench_server.pb.RunRequest(
                code=(0xD2800C80).to_bytes(4, "little"),
                entry_pc=microbench_server.CODE_ENTRY,
                max_instructions=1,
            )
            with (
                mock.patch.object(microbench_server, "_trace_source", source),
                mock.patch.object(microbench_server, "_simulation_runner", runner),
                mock.patch.object(microbench_server, "_trace_cache", trace_cache),
                mock.patch.object(microbench_server, "_result_cache", result_cache),
            ):
                first = microbench_server._run_impl(request)
                second = microbench_server._run_impl(request)
            self.assertEqual(source.calls, 1)
            self.assertEqual(runner.calls, 1)
            self.assertTrue(first.log.startswith("cache=miss"))
            self.assertTrue(second.log.startswith("cache=result"))
            self.assertEqual(
                (first.cycles, first.instructions, first.ipc),
                (second.cycles, second.instructions, second.ipc),
            )


if __name__ == "__main__":
    unittest.main()
