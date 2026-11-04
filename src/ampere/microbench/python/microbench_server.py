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
import hashlib
import os
import subprocess
import sys
import threading
import time
from concurrent import futures
from pathlib import Path

import grpc
from qemu_trace_source import CODE_ENTRY, FLAT_TABLE_PA, QemuTraceSource, TraceError
from simulation_runner import SimulationError, SimulationRunner
from trace_cache import ResultCache, TraceCache, config_fingerprint, file_fingerprint

HERE = Path(__file__).resolve().parent
PROTO_DIR = HERE.parent / "proto"
PROTO = PROTO_DIR / "microbench.proto"
GEN = HERE / "gen"

DEFAULT_LISTEN = "unix:///tmp/panthera-microbench.sock"
SERVICE = "ampere.microbench.Microbench"
MAX_CODE_BYTES = 4 * 1024 * 1024 - 4096
MAX_INSTRUCTIONS = 1_000_000
MAX_BATCH = 64

_trace_source: QemuTraceSource | None = None
_simulation_runner: SimulationRunner | None = None
_trace_cache: TraceCache | None = None
_result_cache: ResultCache | None = None
_run_slots = threading.BoundedSemaphore(4)


def _set_max_concurrent(limit: int):
    global _run_slots
    _run_slots = threading.BoundedSemaphore(limit)


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


def _run_impl(request, timeout_s=None):
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
    if request.max_instructions > MAX_INSTRUCTIONS:
        raise ValueError(f"max_instructions exceeds {MAX_INSTRUCTIONS}")

    started = time.monotonic()
    pipeline = [
        file_fingerprint(Path(__file__)),
        file_fingerprint(Path(__file__).parent / "qemu_trace_source.py"),
        file_fingerprint(Path(__file__).parent / "simulation_runner.py"),
        file_fingerprint(Path(__file__).parent / "trace_cache.py"),
    ]
    trace_key = None
    result_key = None
    if _trace_cache is not None:
        trace_key = _trace_cache.key(
            {
                "code_sha256": hashlib.sha256(request.code).hexdigest(),
                "code_bytes": len(request.code),
                "entry": request.entry_pc,
                "max_instructions": request.max_instructions,
                "qemu": file_fingerprint(_trace_source.qemu),
                "plugin": file_fingerprint(_trace_source.plugin),
                "is_info": file_fingerprint(_trace_source.is_info),
                "code_entry": CODE_ENTRY,
                "flat_table": FLAT_TABLE_PA,
                "pipeline": pipeline,
            }
        )
    if _result_cache is not None and trace_key is not None and _simulation_runner is not None:
        result_key = _result_cache.key(
            {
                "trace_key": trace_key,
                "amperesys": file_fingerprint(_simulation_runner.amperesys),
                "config": config_fingerprint(_simulation_runner.config),
                "pipeline": pipeline,
            }
        )

    lookup_started = time.perf_counter_ns()
    if _result_cache is not None and result_key is not None:
        try:
            cached_result = _result_cache.get(result_key)
        except (OSError, ValueError):
            cached_result = None
        if cached_result is not None:
            return pb.RunResponse(
                cycles=cached_result.cycles,
                instructions=cached_result.instructions,
                ipc=cached_result.ipc,
                wall_ns=time.perf_counter_ns() - lookup_started,
                log=f"cache=result {cached_result.summary()}",
            )

    trace = None
    if _trace_cache is not None and trace_key is not None:
        try:
            trace = _trace_cache.get(trace_key)
        except (OSError, ValueError):
            trace = None
    cache_state = "disabled" if _trace_cache is None else ("trace" if trace is not None else "miss")
    if trace is None:
        trace = _trace_source.run(
            request.code,
            request.max_instructions,
            timeout_s=timeout_s,
        )
        if _trace_cache is not None and trace_key is not None:
            try:
                trace = _trace_cache.put(trace_key, trace)
            except (OSError, ValueError):
                pass

    simulation = None
    if _simulation_runner is not None:
        remaining = None
        if timeout_s is not None:
            remaining = timeout_s - (time.monotonic() - started)
            if remaining <= 0:
                raise TimeoutError("deadline exceeded before simulation")
        simulation = _simulation_runner.run(
            trace.path,
            request.max_instructions,
            timeout_s=remaining,
        )
        if _result_cache is not None and result_key is not None:
            _result_cache.put(result_key, simulation)
    if simulation is not None:
        cycles = simulation.cycles
        instructions = simulation.instructions
        ipc = simulation.ipc
        wall_ns = trace.generation_ns + simulation.wall_ns
        log = f"cache={cache_state} {trace.summary()} {simulation.summary()}"
    else:
        cycles = 0
        instructions = trace.measurement or trace.instructions
        ipc = 0.0
        wall_ns = trace.generation_ns
        log = f"cache={cache_state} {trace.summary()} simulator=disabled"
    return pb.RunResponse(
        cycles=cycles,
        instructions=instructions,
        ipc=ipc,
        wall_ns=wall_ns,
        log=log,
    )


def run(request, context):
    remaining = context.time_remaining()
    if remaining is not None and remaining <= 0:
        context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline exceeded")
    if not _run_slots.acquire(blocking=False):
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "too many concurrent traces")
    try:
        return _run_impl(request, remaining)
    except ValueError as error:
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
    except TimeoutError as error:
        context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, str(error))
    except (TraceError, SimulationError) as error:
        context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))
    finally:
        _run_slots.release()


def run_batch(request, context):
    if len(request.requests) > MAX_BATCH:
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, f"batch size exceeds {MAX_BATCH}")
    if not _run_slots.acquire(blocking=False):
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, "too many concurrent traces")
    try:
        responses = []
        for item in request.requests:
            remaining = context.time_remaining()
            if remaining is not None and remaining <= 0:
                context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline exceeded")
            responses.append(_run_impl(item, remaining))
        return pb.RunBatchResponse(responses=responses)
    except ValueError as error:
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
    except TimeoutError as error:
        context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, str(error))
    except (TraceError, SimulationError) as error:
        context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))
    finally:
        _run_slots.release()


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
    global _trace_source, _simulation_runner
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
        assert "trace=" in response.log or "cache=result" in response.log, response
        assert health_check(address).status == "ok"
    finally:
        server.stop(0).wait()
    print(f"self-test OK: {response.instructions} instruction, wall={response.wall_ns} ns")


def main(argv=None):
    global _trace_source, _simulation_runner, _trace_cache, _result_cache
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen", default=DEFAULT_LISTEN)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="allow a non-Unix listen address (default: Unix socket only)",
    )
    parser.add_argument(
        "--allow-untrusted",
        action="store_true",
        help="accept client code without a sandbox (required with --allow-remote)",
    )
    parser.add_argument("--max-concurrent", type=int, default=4)
    parser.add_argument("--qemu", default=None, help="path to tracing-QEMU qemu-aarch64")
    parser.add_argument("--plugin", default=None, help="path to libinstruction_tracer.so")
    parser.add_argument("--is-info", default=None, help="path to the is_info tool")
    parser.add_argument("--trace-dir", default=None, help="directory for generated traces")
    parser.add_argument("--trace-timeout", type=float, default=120.0)
    parser.add_argument("--keep-intermediates", action="store_true")
    parser.add_argument("--amperesys", default=None, help="path to the amperesys binary")
    parser.add_argument("--config", default=None, help="system config for amperesys")
    parser.add_argument("--sim-timeout", type=float, default=600.0)
    parser.add_argument("--cache-dir", default=None, help="trace/result cache root")
    parser.add_argument("--cache-size-mb", type=int, default=1024)
    parser.add_argument("--no-cache", action="store_true")
    args = parser.parse_args(argv)

    if args.allow_remote and not args.allow_untrusted:
        parser.error(
            "--allow-remote requires --allow-untrusted; user-mode QEMU executes client code with host syscalls"
        )
    if args.max_concurrent < 1:
        parser.error("--max-concurrent must be positive")
    _set_max_concurrent(args.max_concurrent)

    try:
        trace_source = QemuTraceSource(
            qemu=args.qemu,
            plugin=args.plugin,
            is_info=args.is_info,
            workdir=args.trace_dir,
            timeout_s=args.trace_timeout,
            keep_intermediates=args.keep_intermediates,
        )
        try:
            simulation_runner = SimulationRunner(
                amperesys=args.amperesys,
                config=args.config,
                timeout_s=args.sim_timeout,
            )
        except SimulationError as error:
            print(f"warning: simulator runner disabled: {error}", file=sys.stderr)
            simulation_runner = None
        _simulation_runner = simulation_runner
        if args.no_cache:
            _trace_cache = _result_cache = None
        else:
            root = Path(
                args.cache_dir or os.environ.get("PANTHERA_CACHE_DIR") or Path.home() / ".cache/panthera-microbench"
            )
            max_bytes = args.cache_size_mb * 1024 * 1024 if args.cache_size_mb > 0 else None
            _trace_cache = TraceCache(root / "traces", max_bytes)
            _result_cache = ResultCache(root / "results", max_bytes)
        if args.self_test:
            run_self_test(args.listen, allow_remote=args.allow_remote, trace_source=trace_source)
            return 0
        _trace_source = trace_source
        server = serve(args.listen, allow_remote=args.allow_remote, workers=args.max_concurrent)
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
