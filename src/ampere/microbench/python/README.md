# Microbenchmark gRPC sidecar (spike)

Status: Step 0.4 dependency/transport spike. This is **not** the real runner;
it proves the protobuf-defined service round-trips over a local gRPC channel.

## Why a Python sidecar

This host has `grpcio` 1.84 and `protoc` 36.1, but no C++ gRPC toolchain
(`grpc_cpp_plugin`, `grpc++` CMake package) and no Nix environment. The
protocol in `../proto/microbench.proto` is transport-neutral, so the planned
C++ server can replace this sidecar once the Nix environment provides gRPC.
The simulation dominates request latency, so the Python transport overhead is
not material.

## Generate stubs

The scripts generate `gen/microbench_pb2.py` with
`protoc --python_out` on first use. `gen/` and `__pycache__/` are ignored.

## Self-test

    python3 microbench_server.py --self-test

## Run

    python3 microbench_server.py --listen unix:///tmp/panthera-microbench.sock
    python3 microbench_client.py --listen unix:///tmp/panthera-microbench.sock \
        --code-hex d2800c80,d2800001,91000421,f1000400
    python3 microbench_client.py --listen unix:///tmp/panthera-microbench.sock --health

By default only Unix-domain sockets are accepted; pass `--allow-remote` to bind
TCP. Code is capped at 16 MiB, batches at 64 items, and each request must set
`max_instructions` or `max_cycles`.

## Not yet

- No simulation: `Run` echoes the requested instruction-word count.
- `RunBatch` is implemented server-side but has no client flag yet.
- No authentication, sandboxing, or per-request memory limits beyond the code
  size cap; bind to a Unix socket or loopback only.
