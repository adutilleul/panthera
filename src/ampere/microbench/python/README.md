# Microbenchmark gRPC sidecar

Status: Step 1.3 trace source plus the Step 3.2 v1 subprocess runner. `Run`
wraps the submitted AArch64 words in a static harness ELF, generates a
user-mode QEMU instruction trace, and (when configured) runs `amperesys` to
return real `cycles`/`ipc`.

## Why a Python sidecar

This host has `grpcio` 1.84 and `protoc` 36.1, but no C++ gRPC toolchain
(`grpc_cpp_plugin`, `grpc++` CMake package) and no Nix environment. The
protocol in `../proto/microbench.proto` is transport-neutral, so the planned
C++ server can replace this sidecar once the Nix environment provides gRPC.
Trace generation and simulation dominate request latency, so the Python
transport overhead is not material.

## Requirements

- tracing-QEMU 8.0 `qemu-aarch64` with plugins (the Ampere fork)
- `libinstruction_tracer.so` built against that QEMU
- `aarch64-linux-gnu-gcc` for the harness
- `is_info` for trace validation and metadata
- `amperesys` and a system config (for example `siryn.cfg`) for cycles/IPC

Configure paths with environment variables or CLI flags:

```sh
export PANTHERA_QEMU_AARCH64=/path/to/qemu-aarch64
export PANTHERA_TRACER_PLUGIN=/path/to/libinstruction_tracer.so
export PANTHERA_IS_INFO=/path/to/is_info
export PANTHERA_AMPERESYS=/path/to/amperesys
export PANTHERA_CONFIG=/path/to/siryn.cfg
```

The corresponding flags are `--qemu`, `--plugin`, `--is-info`, `--amperesys`,
and `--config`. `is_info` is
required: the trace source rejects a trace whose measurement count is below the
requested `max_instructions`.

## Generate stubs

The scripts generate `gen/microbench_pb2.py` with `protoc --python_out` on
first use. `gen/` and `__pycache__/` are ignored.

## Self-test

```sh
python3 microbench_server.py --self-test --trace-dir /tmp/panthera-traces
```

## Run

```sh
python3 microbench_server.py --listen unix:///tmp/panthera-microbench.sock \
    --trace-dir /tmp/panthera-traces --cache-dir /tmp/panthera-cache
python3 microbench_client.py --listen unix:///tmp/panthera-microbench.sock \
    --code-hex d2800c80,d2800001,91000421,f1000400 --max-instructions 1000
python3 microbench_client.py --listen unix:///tmp/panthera-microbench.sock --health
python3 microbench_client.py --listen unix:///tmp/panthera-microbench.sock --stats
```

By default only Unix-domain sockets are accepted; pass `--allow-remote
--allow-untrusted` to bind TCP. User-mode QEMU executes client code with host
syscalls, so remote exposure without a sandbox is explicitly opt-in. Code is
capped at 4 MiB, batches at 64 items, `max_instructions` at 1,000,000, and
concurrent traces at 4 (`--max-concurrent`) plus 8 queued
(`--queue-depth`). A full queue returns `RESOURCE_EXHAUSTED`; queued requests
respect the RPC deadline, and running trace/simulation subprocesses use the
remaining deadline. SIGTERM/SIGINT terminates in-flight QEMU/`amperesys`
children, cancels queued work, waits up to `--shutdown-grace` seconds, and
removes the socket. Use `--stats` for counters and p50/p95 latency.
`max_instructions` is required for trace generation.

The harness places the code at `0x400100` after a small `_start` prologue that
zeroes `x0`–`x29`, sets a fixed BSS stack, and exits after the code returns; a
self-loop follows in case the benchmark falls through. The trace source waits
for the tracer to remove `<trace>.tmp` (the finalization signal), then
terminates QEMU and validates the trace with `is_info`. Identical code requests
produce byte-identical `.is.xz` and `.mem` files; flat traces omit
process-specific sysregs and emit fixed synthetic TCR/TTBR.

The source also writes `<trace>.mem`, a flat level-1 page table mapping VA
0–1 GiB to PA 0–1 GiB, plus any requested data sections. `RunRequest.data`
sections are placed in the harness ELF at their requested VAs and written to
the `.mem` image; addresses must be in `0xB00000`–1 GiB, total data is capped
at 2 MiB, and sections must not overlap. The client exposes
`--data-hex ADDRESS:HEX`. The trace and `.mem` are directly consumable by
`amperesys`:

```sh
amperesys -cfg <panthera-root>/install/config/model/sys/siryn.cfg \
  -sys.soc_0.core_0.instruction_stream_name <trace>_0.is.xz \
  -sys.soc_0.core_0.gpc.max_instructions 1000
```

`Run` returns real `cycles`, `instructions`, and `ipc` when the `amperesys`
runner is configured (`--amperesys`/`--config` or the environment); otherwise it
returns the trace metadata with `cycles=0`. The runner starts a fresh
`amperesys` per request, so its ~1.4 s startup dominates very small
benchmarks. Cold-path totals measured here: 10k = 1.89 s, 100k = 7.33 s
(trace + simulation).

Results are cached under `--cache-dir` (default
`$XDG_CACHE_HOME/panthera-microbench` or `~/.cache/panthera-microbench`) with a
total size cap (`--cache-size-mb`, default 1024; `--no-cache` disables
caching). The key covers the code, caps, trace options, tool fingerprints, the
transitive config include tree, and the pipeline source files. Responses report
`cache=miss|trace|result`; a result hit returns the original metrics in about
70–90 µs with no QEMU or `amperesys`. Corrupt or incomplete entries are
removed and regenerated.

## Tests

```sh
python3 -m unittest -v test_qemu_trace_source test_simulation_runner test_trace_cache test_worker_pool
```

The QEMU/`amperesys` integration tests run only when
`PANTHERA_QEMU_AARCH64`, `PANTHERA_TRACER_PLUGIN`, `PANTHERA_IS_INFO`,
`PANTHERA_AMPERESYS`, and `PANTHERA_CONFIG` are set; otherwise they skip.

CMake registers the same suite as `microbench_sidecar_python_test` (label
`integration`) when `protoc` and `grpcio` are available:

```sh
ctest --test-dir cmake-build-release -R microbench_sidecar --output-on-failure
```

## Not yet

- No in-process simulation: the v1 runner starts a subprocess `amperesys` per
  request, so its ~1.4 s startup dominates very small benchmarks.
- `RunBatch` is implemented server-side but has no client flag yet.
- No sandbox or authentication. Request limits are a 4 MiB code cap and a
  2 MiB total data cap; bind to a Unix socket or use `--allow-untrusted`
  deliberately.
