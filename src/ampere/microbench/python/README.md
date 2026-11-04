# Microbenchmark gRPC sidecar

Status: Step 1.3 trace source. `Run` now wraps the submitted AArch64 words in a
static harness ELF and generates a user-mode QEMU instruction trace. The
simulator runner is still Step 3.2, so `cycles`/`ipc` are 0 for now.

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

Configure paths with environment variables or CLI flags:

```sh
export PANTHERA_QEMU_AARCH64=/path/to/qemu-aarch64
export PANTHERA_TRACER_PLUGIN=/path/to/libinstruction_tracer.so
export PANTHERA_IS_INFO=/path/to/is_info
```

The corresponding flags are `--qemu`, `--plugin`, and `--is-info`. `is_info` is
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
    --trace-dir /tmp/panthera-traces
python3 microbench_client.py --listen unix:///tmp/panthera-microbench.sock \
    --code-hex d2800c80,d2800001,91000421,f1000400 --max-instructions 1000
python3 microbench_client.py --listen unix:///tmp/panthera-microbench.sock --health
```

By default only Unix-domain sockets are accepted; pass `--allow-remote
--allow-untrusted` to bind TCP. User-mode QEMU executes client code with host
syscalls, so remote exposure without a sandbox is explicitly opt-in. Code is
capped at 4 MiB, batches at 64 items, `max_instructions` at 1,000,000, and
concurrent traces at 4 (`--max-concurrent`). `max_instructions` is required
for trace generation.

The harness places the code at `0x400100` after a small `_start` prologue that
zeroes `x0`–`x29`, sets a fixed BSS stack, and exits after the code returns; a
self-loop follows in case the benchmark falls through. The trace source waits
for the tracer to remove `<trace>.tmp` (the finalization signal), then
terminates QEMU and validates the trace with `is_info`. Identical code requests produce
byte-identical `.is.xz` files; flat traces omit process-specific sysregs.

## Tests

```sh
python3 -m unittest -v test_qemu_trace_source
```

The QEMU integration test runs only when `PANTHERA_QEMU_AARCH64`,
`PANTHERA_TRACER_PLUGIN`, and `PANTHERA_IS_INFO` are set.

## Not yet

- No simulation: `cycles` and `ipc` are 0 until the runner is wired in.
- No data sections (`.data`/`.bss` at request-specified addresses).
- `RunBatch` is implemented server-side but has no client flag yet.
- No sandbox, authentication, or per-request memory limits beyond the code
  size cap; bind to a Unix socket or use `--allow-untrusted` deliberately.
