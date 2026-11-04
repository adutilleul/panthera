#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""End-to-end smoke test for the microbenchmark gRPC sidecar.

Starts the server on a temporary Unix socket, runs one benchmark twice (cold
and result-cache hit), checks ``Stats``, and verifies graceful shutdown. The
test skips (exit 0) when the QEMU/plugin/``amperesys`` environment is absent;
pass ``--require`` in CI to turn that skip into a failure.
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL_ENV = (
    "PANTHERA_QEMU_AARCH64",
    "PANTHERA_TRACER_PLUGIN",
    "PANTHERA_IS_INFO",
    "PANTHERA_AMPERESYS",
    "PANTHERA_CONFIG",
)
# movz x0, #1; movz x1, #2; add x0, x0, x1
CODE = b"".join(word.to_bytes(4, "little") for word in (0xD2800020, 0xD2800041, 0x8B010000))
MAX_INSTRUCTIONS = 3


def tools_available() -> bool:
    if not all(os.environ.get(name) and Path(os.environ[name]).exists() for name in TOOL_ENV):
        return False
    return shutil.which("aarch64-linux-gnu-gcc") is not None


def wait_for_socket(path: Path, timeout_s: float, server: subprocess.Popen) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if path.exists():
            return
        if server.poll() is not None:
            raise RuntimeError(f"server exited with {server.returncode} before creating {path}")
        time.sleep(0.05)
    raise RuntimeError(f"server did not create {path}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--require", action="store_true", help="fail instead of skipping")
    parser.add_argument("--server", default=str(HERE / "microbench_server.py"))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    if not tools_available():
        message = "skipping smoke test: set " + ", ".join(TOOL_ENV)
        print(message, file=sys.stderr)
        return 2 if args.require else 0

    # Imported after the tool check so a skipped run needs no grpcio either.
    from microbench_client import call, health_check, reset_channels, stats_check
    from microbench_server import pb

    with tempfile.TemporaryDirectory(prefix="panthera-microbench-smoke-") as directory:
        root = Path(directory)
        socket_path = root / "server.sock"
        log_path = root / "server.log"
        command = [
            sys.executable,
            args.server,
            "--listen",
            f"unix://{socket_path}",
            "--cache-dir",
            str(root / "cache"),
            "--trace-dir",
            str(root / "traces"),
            "--shutdown-grace",
            "5",
        ]
        result = 0
        with log_path.open("w") as log:
            server = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT)
            try:
                wait_for_socket(socket_path, args.timeout, server)
                address = f"unix://{socket_path}"
                if health_check(address).status != "ok":
                    raise RuntimeError("unhealthy server")

                request = pb.RunRequest(code=CODE, max_instructions=MAX_INSTRUCTIONS)
                cold = call(address, request)
                warm = call(address, request)
                for response in (cold, warm):
                    if response.cycles == 0 or response.instructions == 0:
                        raise RuntimeError(f"no metrics: {response}")
                if (cold.cycles, cold.instructions) != (warm.cycles, warm.instructions):
                    raise RuntimeError("cache hit changed the metrics")
                if "cache=result" not in warm.log:
                    raise RuntimeError(f"second run did not hit the result cache: {warm.log}")

                counters = stats_check(address).counters
                if counters.get("requests", 0) < 2 or counters.get("cache_miss", 0) < 1:
                    raise RuntimeError(f"unexpected counters: {counters}")
                if counters.get("cache_hits", 0) < 1:
                    raise RuntimeError(f"missing result-cache hit: {counters}")

                print(
                    f"smoke OK: cycles={cold.cycles} instructions={cold.instructions} "
                    f"ipc={cold.ipc:.3f} warm={warm.wall_ns} ns"
                )
            finally:
                reset_channels()
                server.send_signal(signal.SIGTERM)
                try:
                    server.wait(timeout=args.timeout)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                if server.returncode != 0:
                    print(log_path.read_text(), file=sys.stderr)
                    result = 1
                if not args.keep and socket_path.exists():
                    print("server did not remove its socket", file=sys.stderr)
                    result = 1
    return result


if __name__ == "__main__":
    sys.exit(main())
