# Microbenchmark gRPC Server

The microbenchmark server accepts AArch64 machine-code bytes over gRPC, runs
them on a CPU-only Panthera configuration, and returns cycle, instruction, and
IPC metrics. It is a Python sidecar around tracing-QEMU and `amperesys`; it
does not boot Linux and does not require a full-system model.

The sidecar is additive: existing `amperesys` and instruction-stream flows are
unchanged.

## How it works

```
gRPC client
    |
    v
microbench_server
  |-- trace/result cache
  |-- bounded worker pool
  |-- tracing-QEMU 8.0 (user mode) + instruction_tracer plugin
  |       -> <trace>.is.xz + <trace>.mem
  `-- amperesys on the trace
          -> cycles / instructions / IPC
```

The trace producer, flat page table, and simulation runner are described
below. A result-cache hit returns the original metrics without QEMU or
`amperesys`.

## Requirements

- tracing-QEMU 8.0 `qemu-aarch64` with plugins (the Ampere fork)
- `libinstruction_tracer.so` built against that QEMU
- `is_info` (trace validation/metadata)
- `amperesys` and a system config (for example `siryn.cfg`)
- Python 3.10+ with `grpcio` and `protoc`
- `aarch64-linux-gnu-gcc` for the harness

Use the Nix development environment when possible; otherwise point the server
at the tools with environment variables or flags.

## Install and run

Build/install Panthera normally. The sidecar installs as
`bin/microbench-server`, with its Python modules under
`lib/python/panthera/microbench/`.

```bash
export PANTHERA_QEMU_AARCH64=/path/to/qemu-aarch64
export PANTHERA_TRACER_PLUGIN=/path/to/libinstruction_tracer.so
export PANTHERA_IS_INFO=/path/to/is_info
export PANTHERA_AMPERESYS=/path/to/amperesys
export PANTHERA_CONFIG=/path/to/siryn.cfg

microbench-server --listen unix:///tmp/panthera-microbench.sock \
    --cache-dir /tmp/panthera-cache \
    --trace-dir /tmp/panthera-traces
```

From a source tree, run
`python3 src/ampere/microbench/python/microbench_server.py` with the same
flags. `PANTHERA_GEN_DIR` controls where generated protobuf stubs are written;
the installed wrapper defaults it to the user cache.

The corresponding flags are `--qemu`, `--plugin`, `--is-info`, `--amperesys`,
and `--config`.

## Request

```proto
message Section { uint64 address = 1; bytes data = 2; }

message RunRequest {
  string config = 1;
  uint64 entry_pc = 2;
  bytes code = 3;
  repeated Section data = 4;
  uint64 max_instructions = 5;
  uint64 max_cycles = 6;
  uint64 warmup_instructions = 7;
  map<string, string> params = 8;
}

message RunResponse {
  uint64 cycles = 1;
  uint64 instructions = 2;
  double ipc = 3;
  uint64 wall_ns = 4;
  map<string, uint64> pmu = 5;
  string log = 6;
}

service Microbench {
  rpc Run(RunRequest) returns (RunResponse);
  rpc RunBatch(RunBatchRequest) returns (RunBatchResponse);
  rpc Health(HealthRequest) returns (HealthResponse);
  rpc Stats(StatsRequest) returns (StatsResponse);
}
```

`code` is little-endian AArch64 words. `max_instructions` is required.
`entry_pc` may be 0 or `0x400100`. `RunBatch` is limited to 64 requests.

Example with the bundled client:

```bash
microbench-client --listen unix:///tmp/panthera-microbench.sock \
    --code-hex d2800c80,d2800001,91000421,f1000400 \
    --max-instructions 1000
```

## Memory layout and data

The harness wraps the code in a static AArch64 ELF and a flat identity page
table. The low 1 GiB is mapped VA=PA:

| Region | Address |
|---|---:|
| Harness/code | `0x400000`–`0x800100` (4 MiB code cap) |
| Flat page table | `0x900000` |
| Harness stack | `0xA00000` |
| User data | `0xB00000`–1 GiB |

`RunRequest.data` sections are placed at their requested addresses in both the
ELF and `<trace>.mem`. Data must not overlap, must be at `0xB00000` or above,
and is limited to 2 MiB in total. Use `--data-hex ADDRESS:HEXBYTES` on the
client.

The harness zeroes `x0`–`x29`, sets a fixed stack, calls the code, and exits
after it returns; a self-loop follows if the code falls through. The trace
includes 1000 cooldown instructions so `amperesys` can drain short benchmarks.

## Limits and caching

- Code: 4 MiB; data: 2 MiB total; `max_instructions`: 1,000,000.
- `--max-concurrent` workers plus `--queue-depth` queued requests. A full
  queue returns `RESOURCE_EXHAUSTED`; queued work respects the RPC deadline.
- SIGTERM/SIGINT terminates in-flight QEMU/`amperesys` children, cancels
  queued work, waits up to `--shutdown-grace`, and removes the socket.
- `--cache-dir` (XDG-aware), `--cache-size-mb` (total, default 1024), and
  `--no-cache`. Keys cover code, caps, data, tool fingerprints, the transitive
  config include tree, and pipeline sources.
- Result-cache hits take ~0.1 ms of server processing (~2 ms client
  round-trip on a persistent channel). Trace-cache hits skip QEMU but still
  simulate.

## Health, stats, and shutdown

```bash
microbench-client --listen unix:///tmp/panthera-microbench.sock --health
microbench-client --listen unix:///tmp/panthera-microbench.sock --stats
```

`Stats` reports request, queue, cache, trace, simulation, rejection, deadline,
and error counters plus p50/p95/p99 request latency.

## Security

The server executes client-supplied machine code. v1 is trusted-local: bind a
Unix socket or loopback. `--allow-remote` additionally requires
`--allow-untrusted`; user-mode QEMU can make host syscalls, and a sandbox is a
follow-up.

## Supported subset and limitations

- EL0 user-mode integer/scalar-FP workloads are the target. System calls,
  exceptions, sysreg-heavy code, and SVE/SME are outside the v1 subset.
- The QEMU trace producer uses the Ampere tracing-QEMU fork.
- `RunResponse.pmu` is not populated yet.
- The subprocess `amperesys` runner starts a fresh simulator per cold request;
  a C++ in-process runner is an optional performance follow-up.
- `RunBatch` is available but the client has no batch flag yet.

## Tests

```bash
cd src/ampere/microbench/python
python3 -m unittest -v test_qemu_trace_source test_simulation_runner \
    test_trace_cache test_worker_pool
```

The QEMU/`amperesys` integration tests skip unless the `PANTHERA_*` tool
environment is set. CTest registers the suite as
`microbench_sidecar_python_test` (label `integration`).

For a single end-to-end check (start the server, run cold and cached requests,
check `Stats`, and shut down):

```bash
python3 smoke_test.py            # skips without PANTHERA_* tools
python3 smoke_test.py --require  # CI: fail instead of skipping
```
