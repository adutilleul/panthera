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
from qemu_trace_source import CODE_ENTRY, QemuTraceSource, TraceError

HERE = Path(__file__).resolve().parent
PROTO_DIR = HERE.parent / "proto"
PROTO = PROTO_DIR / "microbench.proto"
GEN = HERE / "gen"

DEFAULT_LISTEN = "unix:///tmp/panthera-microbench.sock"
SERVICE = "ampere.microbench.Microbench"
MAX_CODE_BYTES = 16 * 1024 * 1024
MAX_BATCH = 64

_trace_source: QemuTraceSource | None = None


def ensure_generated():
    """Generate the message stubs if they are missing or stale."""
    GEN.mkdir(exist_ok=True)
    generated = GEN / "microbench_pb2.py"
    if not generated.exists() or generated.stat().st_mtime < PROTO.stat().st_mtime:
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
    if _trace_source is None:
        raise TraceError("trace source is not configured")
    if not request.code:
        raise ValueError("code is empty")
    if len(request.code) % 4 != 0:
        raise ValueError("code length must be a multiple of 4 bytes")
    if len(request.code) > MAX_CODE_BYTES:
        raise ValueError(f"code exceeds {MAX_CODE_BYTES} bytes")
    if request.data:
        raise ValueError("data sections are not supported yet")
    if request.entry_pc not in (0, CODE_ENTRY):
        raise ValueError(f"entry_pc must be 0 or {CODE_ENTRY:#x}")
    if request.max_instructions == 0:
        raise ValueError("max_instructions is required for trace generation")

    result = _trace_source.run(
        request.code,
        request.max_instructions,
        name=f"run_{time.time_ns()}",
    )
    return pb.RunResponse(
        cycles=0,
        instructions=result.measurement or result.instructions,
        ipc=0.0,
        wall_ns=result.generation_ns,
        log=result.summary(),
    )


def run(request, context):
    try:
        return _run_impl(request)
    except ValueError as error:
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
    except TraceError as error:
        context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))


def run_batch(request, context):
    if len(request.requests) > MAX_BATCH:
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, f"batch size exceeds {MAX_BATCH}")
    try:
        return pb.RunBatchResponse(responses=[_run_impl(item) for item in request.requests])
    except ValueError as error:
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
    except TraceError as error:
        context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))


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


def serve(address, workers=4, allow_remote=False):
    socket_path = None
    for prefix in ("unix://", "unix:"):
        if address.startswith(prefix):
            socket_path = address[len(prefix) :]
            break
    if socket_path is None and not allow_remote:
        raise ValueError("refusing a non-Unix listen address without --allow-remote")
    if socket_path:
        path = Path(socket_path)
        if path.is_socket():
            path.unlink()
        elif path.exists():
            raise ValueError(f"{socket_path} exists and is not a socket")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    server.add_generic_rpc_handlers((_generic_handler(),))
    if server.add_insecure_port(address) == 0:
        raise RuntimeError(f"failed to bind {address}")
    server.start()
    return server


def run_self_test(address, allow_remote=False, trace_source=None):
    global _trace_source
    from microbench_client import call, health_check  # noqa: PLC0415

    _trace_source = trace_source
    server = serve(address, allow_remote=allow_remote)
    try:
        response = call(
            address,
            pb.RunRequest(
                entry_pc=CODE_ENTRY,
                code=b"\x80\x0c\x80\xd2",  # mov x0, #100
                max_instructions=1,
            ),
        )
        assert response.instructions == 1, response
        assert "trace=" in response.log, response
        assert health_check(address).status == "ok"
    finally:
        server.stop(0).wait()
    print(f"self-test OK: {response.instructions} instruction, wall={response.wall_ns} ns")


def main(argv=None):
    global _trace_source
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default=DEFAULT_LISTEN)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-Unix listen address (default: Unix socket only)",
    )
    parser.add_argument("--qemu", default=None, help="path to tracing-QEMU qemu-aarch64")
    parser.add_argument("--plugin", default=None, help="path to libinstruction_tracer.so")
    parser.add_argument("--is-info", default=None, help="path to the is_info tool")
    parser.add_argument("--trace-dir", default=None, help="directory for generated traces")
    parser.add_argument("--trace-timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    try:
        trace_source = QemuTraceSource(
            qemu=args.qemu,
            plugin=args.plugin,
            is_info=args.is_info,
            workdir=args.trace_dir,
            timeout_s=args.trace_timeout,
        )
        if args.self_test:
            run_self_test(args.listen, allow_remote=args.allow_remote, trace_source=trace_source)
            return 0
        _trace_source = trace_source
        server = serve(args.listen, allow_remote=args.allow_remote)
    except ValueError as error:
        parser.error(str(error))
    except TraceError as error:
        parser.error(str(error))
    print(f"listening on {args.listen}", flush=True)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        server.stop(0).wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
