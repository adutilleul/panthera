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
import signal
import subprocess
import sys
import threading
import time
from collections import defaultdict, deque
from concurrent import futures
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import grpc
from process_registry import terminate_all
from qemu_trace_source import CODE_ENTRY, FLAT_TABLE_PA, QemuTraceSource, TraceError, validate_data_sections
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
_executor: ThreadPoolExecutor | None = None
_admission: threading.BoundedSemaphore | None = None


class AdmissionError(RuntimeError):
    pass


class Metrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._counters = defaultdict(int)
        self._durations_ms = deque(maxlen=1024)

    def inc(self, name: str, value: int = 1):
        with self._lock:
            self._counters[name] += value

    def dec(self, name: str, value: int = 1):
        with self._lock:
            self._counters[name] -= value

    def observe_ms(self, value: float):
        with self._lock:
            self._durations_ms.append(value)

    def snapshot(self):
        with self._lock:
            counters = dict(self._counters)
            counters["active"] = counters.get("running", 0)
            counters["queued"] = counters.get("queued", 0)
            counters["cache_hits"] = counters.get("cache_result", 0) + counters.get("cache_trace", 0)
            values = sorted(self._durations_ms)

            def percentile(p):
                if not values:
                    return 0.0
                index = min(len(values) - 1, max(0, int(round((p / 100.0) * (len(values) - 1)))))
                return values[index]

            timings = {"request_p50_ms": percentile(50), "request_p95_ms": percentile(95)}
        return counters, timings


_metrics = Metrics()


def _init_workers(workers: int, queue_depth: int):
    global _executor, _admission, _metrics
    if _executor is not None:
        _executor.shutdown(wait=False)
    _metrics = Metrics()
    _admission = threading.BoundedSemaphore(workers + queue_depth)
    _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="microbench")


def _submit(work):
    if _executor is None or _admission is None:
        raise RuntimeError("worker pool is not initialized")
    if not _admission.acquire(blocking=False):
        raise AdmissionError("worker queue is full")
    _metrics.inc("admitted")
    _metrics.inc("queued")
    submitted = time.perf_counter_ns()

    def job():
        _metrics.dec("queued")
        _metrics.inc("running")
        try:
            return work()
        finally:
            _metrics.dec("running")
            _metrics.inc("finished")
            _metrics.observe_ms((time.perf_counter_ns() - submitted) / 1e6)
            _admission.release()

    try:
        future = _executor.submit(job)
    except Exception:
        _metrics.dec("queued")
        _admission.release()
        raise

    def release_cancelled(done):
        # A Future cancelled before it starts never runs job(), so its
        # admission permit and queued accounting must be released here.
        if done.cancelled():
            _metrics.dec("queued")
            _metrics.inc("cancelled")
            _admission.release()

    future.add_done_callback(release_cancelled)
    return future


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
    try:
        data_sections = validate_data_sections([(section.address, section.data) for section in request.data])
    except TraceError as error:
        raise ValueError(str(error)) from error
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
                "data": [
                    {
                        "address": address,
                        "size": len(data),
                        "sha256": hashlib.sha256(data).hexdigest(),
                    }
                    for address, data in data_sections
                ],
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
            _metrics.inc("cache_result")
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
    _metrics.inc(f"cache_{cache_state}")
    if trace is None:
        _metrics.inc("trace_runs")
        trace = _trace_source.run(
            request.code,
            request.max_instructions,
            data_sections=data_sections,
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
        _metrics.inc("sim_runs")
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


def _map_job_error(context, error):
    if isinstance(error, TimeoutError):
        _metrics.inc("deadline_exceeded")
        context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, str(error))
    if isinstance(error, ValueError):
        _metrics.inc("invalid_requests")
        context.abort(grpc.StatusCode.INVALID_ARGUMENT, str(error))
    if isinstance(error, (TraceError, SimulationError)):
        _metrics.inc("failed_precondition")
        context.abort(grpc.StatusCode.FAILED_PRECONDITION, str(error))
    _metrics.inc("internal_errors")
    context.abort(grpc.StatusCode.INTERNAL, str(error))


def run(request, context):
    _metrics.inc("requests")
    remaining = context.time_remaining()
    if remaining is not None and remaining <= 0:
        _metrics.inc("deadline_exceeded")
        context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline exceeded")
    deadline = None if remaining is None else time.monotonic() + remaining

    def work():
        job_timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
        return _run_impl(request, job_timeout)

    try:
        future = _submit(work)
    except AdmissionError as error:
        _metrics.inc("rejected_full")
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(error))
    try:
        return future.result(timeout=remaining)
    except Exception as error:  # noqa: BLE001 - mapped to a gRPC status below
        future.cancel()
        _map_job_error(context, error)


def run_batch(request, context):
    _metrics.inc("requests")
    if len(request.requests) > MAX_BATCH:
        _metrics.inc("rejected_batch")
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, f"batch size exceeds {MAX_BATCH}")
    remaining = context.time_remaining()
    if remaining is not None and remaining <= 0:
        _metrics.inc("deadline_exceeded")
        context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, "deadline exceeded")

    deadline = None if remaining is None else time.monotonic() + remaining

    def work():
        responses = []
        for item in request.requests:
            item_timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            if item_timeout is not None and item_timeout <= 0:
                raise TimeoutError("deadline exceeded")
            responses.append(_run_impl(item, item_timeout))
        return pb.RunBatchResponse(responses=responses)

    try:
        future = _submit(work)
    except AdmissionError as error:
        _metrics.inc("rejected_full")
        context.abort(grpc.StatusCode.RESOURCE_EXHAUSTED, str(error))
    try:
        return future.result(timeout=remaining)
    except Exception as error:  # noqa: BLE001 - mapped to a gRPC status below
        future.cancel()
        _map_job_error(context, error)


def health(request, context):
    return pb.HealthResponse(status="ok", version="spike")


def stats(request, context):
    counters, timings = _metrics.snapshot()
    return pb.StatsResponse(counters=counters, timings=timings)


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
            "Stats": grpc.unary_unary_rpc_method_handler(
                stats,
                request_deserializer=pb.StatsRequest.FromString,
                response_serializer=pb.StatsResponse.SerializeToString,
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

    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=workers),
        options=[
            ("grpc.max_receive_message_length", 8 * 1024 * 1024),
            ("grpc.max_send_message_length", 8 * 1024 * 1024),
        ],
    )
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
    parser.add_argument("--queue-depth", type=int, default=8)
    parser.add_argument("--shutdown-grace", type=float, default=10.0)
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
    if args.queue_depth < 0:
        parser.error("--queue-depth must be non-negative")
    if args.shutdown_grace < 0:
        parser.error("--shutdown-grace must be non-negative")
    _init_workers(args.max_concurrent, args.queue_depth)

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
            xdg_cache = os.environ.get("XDG_CACHE_HOME")
            cache_base = Path(xdg_cache) if xdg_cache else Path.home() / ".cache"
            root = Path(args.cache_dir or os.environ.get("PANTHERA_CACHE_DIR") or cache_base / "panthera-microbench")
            max_bytes = args.cache_size_mb * 1024 * 1024 if args.cache_size_mb > 0 else None
            per_cache = max_bytes // 2 if max_bytes is not None else None
            _trace_cache = TraceCache(root / "traces", per_cache)
            _result_cache = ResultCache(root / "results", per_cache)
        if args.self_test:
            run_self_test(args.listen, allow_remote=args.allow_remote, trace_source=trace_source)
            if _executor is not None:
                _executor.shutdown(wait=True)
            return 0
        _trace_source = trace_source
        server = serve(
            args.listen,
            allow_remote=args.allow_remote,
            workers=min(64, args.max_concurrent + args.queue_depth + 4),
        )
    except ValueError as error:
        parser.error(str(error))
    except TraceError as error:
        parser.error(str(error))

    socket_path = None
    for prefix in ("unix://", "unix:"):
        if args.listen.startswith(prefix):
            socket_path = args.listen[len(prefix) :]
            break

    def handle_shutdown(signum, frame):
        # Stop accepting immediately, cancel queued work, terminate in-flight
        # QEMU/amperesys children, then wait at most shutdown-grace for the
        # worker threads to unwind.
        server.stop(grace=0)
        terminate_started = time.monotonic()
        terminate_all(min(2.0, args.shutdown_grace))
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
        remaining = max(0.0, args.shutdown_grace - (time.monotonic() - terminate_started))
        deadline = time.monotonic() + remaining
        while time.monotonic() < deadline:
            counters, _ = _metrics.snapshot()
            if counters.get("active", 0) == 0:
                break
            time.sleep(0.05)
        if socket_path:
            Path(socket_path).unlink(missing_ok=True)
        os._exit(0)

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    print(f"listening on {args.listen}", flush=True)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        handle_shutdown(None, None)
    finally:
        if _executor is not None:
            _executor.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
