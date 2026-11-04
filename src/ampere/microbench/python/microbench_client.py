#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Client for the microbenchmark gRPC sidecar spike."""

import argparse
import sys

import grpc
from microbench_server import DEFAULT_LISTEN, SERVICE, pb


def _call(address, method_name, request, request_type, response_type, timeout=5):
    channel = grpc.insecure_channel(address)
    try:
        grpc.channel_ready_future(channel).result(timeout=timeout)
        method = channel.unary_unary(
            f"/{SERVICE}/{method_name}",
            request_serializer=request_type.SerializeToString,
            response_deserializer=response_type.FromString,
        )
        return method(request, timeout=timeout)
    finally:
        channel.close()


def call(address, request, timeout=5):
    return _call(address, "Run", request, pb.RunRequest, pb.RunResponse, timeout)


def health_check(address, timeout=5):
    return _call(address, "Health", pb.HealthRequest(), pb.HealthRequest, pb.HealthResponse, timeout)


def stats_check(address, timeout=5):
    return _call(address, "Stats", pb.StatsRequest(), pb.StatsRequest, pb.StatsResponse, timeout)


def parse_code(text):
    words = [int(word, 16) for word in text.replace(" ", "").split(",") if word]
    return b"".join(word.to_bytes(4, "little") for word in words)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default=DEFAULT_LISTEN)
    parser.add_argument("--code-hex", default="d2800c80", help="comma-separated 32-bit words")
    parser.add_argument("--entry-pc", type=lambda value: int(value, 0), default=0x400100)
    parser.add_argument("--max-instructions", type=int, default=1000)
    parser.add_argument("--health", action="store_true")
    parser.add_argument("--stats", action="store_true")
    args = parser.parse_args(argv)

    if args.health:
        print(health_check(args.listen))
        return 0
    if args.stats:
        response = stats_check(args.listen)
        print("counters:", dict(response.counters))
        print("timings:", dict(response.timings))
        return 0

    try:
        code = parse_code(args.code_hex)
    except ValueError:
        parser.error(f"invalid --code-hex: {args.code_hex}")

    response = call(
        args.listen,
        pb.RunRequest(
            entry_pc=args.entry_pc,
            code=code,
            max_instructions=args.max_instructions,
        ),
    )
    print(
        f"cycles={response.cycles} instructions={response.instructions} "
        f"ipc={response.ipc:.3f} wall_ns={response.wall_ns} log={response.log}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
