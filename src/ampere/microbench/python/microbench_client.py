#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Client for the microbenchmark gRPC sidecar spike."""

import argparse
import sys

import grpc
from microbench_server import DEFAULT_LISTEN, SERVICE, pb


def _channel(address, timeout=5):
    channel = grpc.insecure_channel(address)
    grpc.channel_ready_future(channel).result(timeout=timeout)
    return channel


def call(address, request, timeout=5):
    channel = _channel(address, timeout)
    try:
        method = channel.unary_unary(
            f"/{SERVICE}/Run",
            request_serializer=pb.RunRequest.SerializeToString,
            response_deserializer=pb.RunResponse.FromString,
        )
        return method(request, timeout=timeout)
    finally:
        channel.close()


def health_check(address, timeout=5):
    channel = _channel(address, timeout)
    try:
        method = channel.unary_unary(
            f"/{SERVICE}/Health",
            request_serializer=pb.HealthRequest.SerializeToString,
            response_deserializer=pb.HealthResponse.FromString,
        )
        return method(pb.HealthRequest(), timeout=timeout)
    finally:
        channel.close()


def parse_code(text):
    words = [int(word, 16) for word in text.replace(" ", "").split(",") if word]
    return b"".join(word.to_bytes(4, "little") for word in words)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default=DEFAULT_LISTEN)
    parser.add_argument("--code-hex", default="d2800c80", help="comma-separated 32-bit words")
    parser.add_argument("--entry-pc", type=lambda value: int(value, 0), default=0x400000)
    parser.add_argument("--max-instructions", type=int, default=1000)
    parser.add_argument("--health", action="store_true")
    args = parser.parse_args(argv)

    if args.health:
        print(health_check(args.listen))
        return 0

    response = call(
        args.listen,
        pb.RunRequest(
            entry_pc=args.entry_pc,
            code=parse_code(args.code_hex),
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
