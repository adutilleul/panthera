#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Barebone gRPC sidecar spike for the microbenchmark server.

This is the Step 0.4 dependency/transport spike, not the real runner. It
proves that a protobuf-defined `Run`/`RunBatch`/`Health` service round-trips
over a local gRPC channel. The simulator runner is wired in Step 3.2.

The Python sidecar is used because this host has no C++ gRPC toolchain
(`grpc_cpp_plugin`, `grpc++` CMake package) and no Nix environment. The
protocol and service shape are the same as the planned C++ server.
"""

import argparse
import subprocess
import sys
import time
from concurrent import futures
from pathlib import Path

import grpc

HERE = Path(__file__).resolve().parent
PROTO_DIR = HERE.parent / "proto"
PROTO = PROTO_DIR / "microbench.proto"
GEN = HERE / "gen"

DEFAULT_LISTEN = "unix:///tmp/panthera-microbench.sock"
SERVICE = "ampere.microbench.Microbench"


def ensure_generated():
    """Generate the message stubs if they are not present."""
    GEN.mkdir(exist_ok=True)
    if not (GEN / "microbench_pb2.py").exists():
        subprocess.run(
            ["protoc", f"--python_out={GEN}", f"-I{PROTO_DIR}", str(PROTO)],
            check=True,
        )
    if str(GEN) not in sys.path:
        sys.path.insert(0, str(GEN))
    import microbench_pb2  # noqa: PLC0415

    return microbench_pb2


pb = ensure_generated()


def _run_impl(request):
    started = time.perf_counter_ns()
    if not request.code:
        raise ValueError("code is empty")
    if len(request.code) % 4 != 0:
        raise ValueError("code length must be a multiple of 4 bytes")
    words = len(request.code) // 4
    return pb.RunResponse(
        cycles=words,
        instructions=words,
        ipc=1.0,
        wall_ns=time.perf_counter_ns() - started,
        log=f"spike: {words} instruction words, entry=0x{request.entry_pc:x}",
    )


def run(request, context):
    try:
        return _run_impl(request)
    except ValueError as error:
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))


def run_batch(request, context):
    try:
        return pb.RunBatchResponse(responses=[_run_impl(item) for item in request.requests])
    except ValueError as error:
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))


def health(request, context):
    return pb.HealthResponse(status="ok", version="spike")


def _generic_handler():
    return grpc.method_handlers_generic_handler(
        SERVICE,
        {
            "Run": grpc.unary_unary_rpc_method_handler(
                run,
                request_deserializer=pb.RunRequest.FromString,
                response_serializer=pb.RunResponse.SerializeToString,
            ),
            "RunBatch": grpc.unary_unary_rpc_method_handler(
                run_batch,
                request_deserializer=pb.RunBatchRequest.FromString,
                response_serializer=pb.RunBatchResponse.SerializeToString,
            ),
            "Health": grpc.unary_unary_rpc_method_handler(
                health,
                request_deserializer=pb.HealthRequest.FromString,
                response_serializer=pb.HealthResponse.SerializeToString,
            ),
        },
    )


def serve(address, workers=4):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    server.add_generic_rpc_handlers((_generic_handler(),))
    if server.add_insecure_port(address) == 0:
        raise RuntimeError(f"failed to bind {address}")
    server.start()
    return server


def run_self_test(address):
    from microbench_client import call, health_check  # noqa: PLC0415

    server = serve(address)
    try:
        response = call(
            address,
            pb.RunRequest(
                entry_pc=0x400000,
                code=b"\x80\x0c\x80\xd2",  # mov x0, #100
                max_instructions=1,
            ),
        )
        assert response.instructions == 1, response
        assert response.cycles == 1, response
        assert health_check(address).status == "ok"
    finally:
        server.stop(0).wait()
    print(f"self-test OK: {response.instructions} instruction, wall={response.wall_ns} ns")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default=DEFAULT_LISTEN)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        run_self_test(args.listen)
        return 0

    server = serve(args.listen)
    print(f"listening on {args.listen}", flush=True)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(0).wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
