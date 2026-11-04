#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Tests for the bounded worker pool and metrics."""

import threading
import unittest

import microbench_server


class MetricsTest(unittest.TestCase):
    def test_counters_and_percentiles(self):
        metrics = microbench_server.Metrics()
        metrics.inc("requests", 2)
        metrics.inc("admitted", 2)
        metrics.inc("running", 1)
        metrics.observe_ms(10.0)
        metrics.observe_ms(20.0)
        counters, timings = metrics.snapshot()
        self.assertEqual(counters["requests"], 2)
        self.assertEqual(counters["active"], 1)
        self.assertEqual(counters["queued"], 1)
        self.assertEqual(timings["request_p50_ms"], 10.0)
        self.assertEqual(timings["request_p95_ms"], 20.0)

    def test_stats_response(self):
        microbench_server._metrics.inc("requests")
        response = microbench_server.stats(None, None)
        self.assertGreaterEqual(response.counters["requests"], 1)


class WorkerPoolTest(unittest.TestCase):
    def setUp(self):
        microbench_server._init_workers(1, 1)  # one worker plus one queued job

    def tearDown(self):
        microbench_server._executor.shutdown(wait=True)

    def test_submit_result(self):
        future = microbench_server._submit(lambda: 42)
        self.assertEqual(future.result(timeout=2), 42)

    def test_admission_rejects_when_full(self):
        release = threading.Event()
        first = microbench_server._submit(lambda: release.wait(2))
        second = microbench_server._submit(lambda: None)
        with self.assertRaises(microbench_server.AdmissionError):
            microbench_server._submit(lambda: None)
        release.set()
        first.result(timeout=2)
        second.result(timeout=2)


if __name__ == "__main__":
    unittest.main()
