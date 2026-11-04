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
- optional: `is_info` for trace metadata

Configure paths with environment variables or CLI flags:

```sh
export PANTHERA_QEMU_AARCH64=/path/to/qemu-aarch64
export PANTHERA_TRACER_PLUGIN=/path/to/libinstruction_tracer.so
export PANTHERA_IS_INFO=/path/to/is_info
```

The corresponding flags are `--qemu`, `--plugin`, and `--is-info`.

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

By default only Unix-domain sockets are accepted; pass `--allow-remote` to bind
TCP. Code is capped at 16 MiB and batches at 64 items. `max_instructions` is
required for trace generation.

The harness places the code at `0x400100` after a small `_start` prologue that
zeroes the register file and exits after the code returns; a self-loop follows
in case the benchmark falls through. The response returns the measured
instruction count, wall time, and a `trace=...` summary. Memory/data sections
are not supported yet.

## Not yet

- No simulation: `cycles` and `ipc` are 0 until the runner is wired in.
- No data sections (`.data`/`.bss` at request-specified addresses).
- `RunBatch` is implemented server-side but has no client flag yet.
- No authentication, sandboxing, or per-request memory limits beyond the code
  size cap; bind to a Unix socket or loopback only.
