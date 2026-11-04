# Microbenchmark gRPC Server — Implementation Plan

Status: working plan (not yet a committed design)
Audience: Panthera developers implementing the feature
Last updated: 2026-11-04

> This is an execution plan, not user-facing documentation. It is intentionally
> not added to `mkdocs.yml` while it is a working plan. Add a nav entry only if
> it is promoted to a permanent design document.

## 1. Objective

Add a small gRPC service that accepts AArch64 machine-code bytes plus optional
data sections, runs a microbenchmark on a CPU-only Panthera configuration, and
returns cycle/instruction/PMU results — without booting Linux or a full-system
model.

Primary success criteria:

- End-to-end request for a 10k–100k instruction microbenchmark completes in
  tens of milliseconds on a **result-cache hit**; seconds on a trace-cache hit
  (simulation-bound). See the revised targets below.
- Throughput scales linearly with worker processes.
- Results are deterministic for identical inputs and configuration.
- Existing `amperesys` / instruction-stream flows are unchanged.
- QEMU remains the trace producer and correctness oracle where possible.

Numeric targets: **revised after Step 0.1** (section 9). Measurement shows the
SystemC model runs at ~10–14 kips on long runs (~9.5–11 kips at 10k/30k); the
fixed cost for a microbench-sized trace is ~1.7 s. The original "tens of
milliseconds" target was based on a wrong assumption about trace generation.

| Metric | 10k instructions | 100k instructions | 1M instructions |
|---|---:|---:|---:|
| Result-cache hit p50 | <= 5 ms | <= 5 ms | <= 5 ms |
| Simulate path (no QEMU), p50 | ~2.9 s | ~11.5 s | ~77 s |
| Cold request (QEMU + simulation) p50 | TBD; user-mode producer available | TBD | TBD |
| Throughput per worker | ~0.35 req/s | ~0.09 req/s | ~0.013 req/s |

Basis: 10k/30k rates measured on `domino.cfg` (~9.5–11 kips); long-trace rates
on `siryn.cfg` (10.6–13.6 kips). The target table mixes these bases.

Sub-second responses require both a small benchmark (<= ~2k instructions) and
removal of the ~1.65 s construct+setup floor (Step 1.6 fork candidate); neither
alone is sufficient. The server should cache **results**, not just traces
(Step 1.2).

Non-goals for v1:

- Full-system / Linux boot.
- Multi-vCPU, MMU-heavy, SVE/SME, or atomics-heavy workloads.
- In-process reset of an already-elaborated model.
- Remote/multi-tenant authentication.
- Untrusted code. v1 accepts client bytes only from trusted local clients; see
  the security boundary in section 3.1.

## 2. Constraints and findings (drivers of the design)

1. Panthera is **trace-driven**, not an ISA simulator. `Core` consumes
   `archx::Driver` instructions; it never fetches from memory.
   - `src/ampere/archx/driver.h`
   - `src/ampere/archx/driver_is.cc`
   - `src/ampere/instruction_stream/instruction_stream.proto`
2. `Memory` / `MemResponder` are timing-only. Functional data must come from
   `ArchState` (`src/ampere/archx/arch_state.h`).
3. `archx::emulate` already functionally emulates ~217 encoding handlers and is
   validated against QEMU traces via `validate_emulation`
   (`src/ampere/archx/emulate.cc`, `src/ampere/core/test/test_emulation.cc`).
   Coverage gaps: SVE/SME, some FP, some atomics/exclusives.
4. `SocForCorePV` is the minimal CPU + fixed-latency memory configuration
   (`src/ampere/soc/soc_for_core_pv.cc`).
5. A `Sys` can be constructed and destroyed repeatedly inside one process with
   a fresh `sc_core::sc_simcontext`; `IntegrationTest` already does this
   (`src/ampere/sc/testing/test_module.h`).
6. SystemC 2.3.3 uses process-global state; `sc_stop()` is terminal. One
   simulation at a time per process; parallelism must be process-based.
7. The Nix QEMU package builds both `aarch64-softmmu` and `aarch64-linux-user`
   with plugins (`nix/pkgs/qemu/default.nix`), so user-mode QEMU without Linux
   boot is available.
8. `ProtobufStreamWriter`/`Reader` already support raw file descriptors, but
   `InstructionStreamReader` only accepts a path
   (`src/ampere/protobuf_utils/protobuf_stream.h`). The `.xz` extension selects
   the compressed path (`protobuf_stream.cc:34`), which spawns an `xz`
   subprocess in `util::open_xz_{output,input}_file`
   (`src/ampere/util/compressed_fstream.cc`); non-`.xz` is uncompressed.
9. The tree currently contains unrelated uncommitted work, including tracked
   edits to `CMakeLists.txt`, `mkdocs.yml`, and
   `src/ampere/core/test/CMakeLists.txt`, plus `mips_bench` and
   `src/ampere/core/test/microbench/`. Several tracked files overlap this
   plan's file map, so all work must happen in a dedicated worktree (section
   4.1) and must never be committed together with that work.

## 3. Architecture

```
gRPC client
    |
    v
MicrobenchServer
  |-- RequestValidator / config resolver
  |-- TraceCache  (content hash -> .is + memory image)
  |-- ResultCache (content hash -> final metrics; makes repeats fast)
  |-- TraceSource (strategy)
  |     |-- CachedTraceSource
  |     |-- QemuTraceSource  (user-mode first, bare-metal system-mode fallback)
  |     `-- InProcessTraceSource (DriverCode; phase 2, gated)
  |-- SimulationRunner (fresh simcontext + Sys + EventStream collector)
  `-- WorkerPool / process supervisor
```

Core contracts to define early (headers first, implement later):

```cpp
struct TraceRequest {
    std::vector<uint8_t> code;              // little-endian AArch64 words
    uint64_t entry_pc;
    std::vector<Section> data;              // address + bytes
    uint64_t max_instructions;
    uint64_t max_cycles;
    uint64_t warmup_instructions;
    std::string config;
    std::map<std::string, std::string> params;
};

struct TraceResult {
    std::filesystem::path instruction_stream;  // or fd
    std::filesystem::path memory_image;
    TraceMetrics metrics;                      // generation timings, cache hit
};

class TraceSource {
  public:
    virtual ~TraceSource() = default;
    virtual TraceResult produce(const TraceRequest&) = 0;
};
```

Keep the trace source and the simulation runner separable so the hot path can
switch from QEMU to in-process without changing the RPC layer.

### 3.1 Security boundary

The server executes arbitrary client-supplied machine code. Treat this as
hostile even for an internal "barebone" tool:

- **v1 scope:** trusted local clients only; bind to a Unix-domain socket or
  loopback. Network exposure requires an explicit opt-in flag and the sandbox
  below.
- **QEMU user-mode:** the guest can issue host syscalls. Run QEMU workers in a
  restricted sandbox (seccomp/namespaces/container) with a read-only view of
  the cache directory, or use bare-metal system-mode with no host syscalls.
- **In-process driver (phase 2):** no host syscalls, but still bound memory and
  instruction counts; a malformed request must kill and replace the worker, not
  corrupt the server.
- **Limits:** max message size, max code/data bytes, max instructions/cycles,
  request deadline, and queue depth. Reject; do not queue unboundedly.
- **Provenance:** log a hash of the code and the resolved config for every run.

This section is a requirement, not an open question. Revisit it before any
network exposure.

## 4. Mandatory subagent workflow

This project is large enough that logs, greps, and reviews will pollute the main
conversation. Use subagents for **all** of the following.

### 4.1 Rules

- **One step = one coherent change.** Do not start step N+1 until step N has
  passed its review gate and has been committed (or explicitly parked).
- **After every step, spawn a review subagent before continuing.** No
  exceptions, including small steps. The review is a gate.
- **Delegate menial work** to subagents: running builds/tests/formatters,
  collecting timings, reading long logs, locating symbols, checking generated
  files, producing diff stats. The main agent consumes only the summary.
- **Never paste raw build/test logs into the main context.** The subagent must
  return exit status, failing test names, and at most ~20 lines of the first
  relevant error.
- **Code changes stay in the main context** unless the edit is purely
  mechanical and isolated (for example adding a file to a `CMakeLists.txt`).
  Even then, review the resulting diff.
- **Dedicated worktree is mandatory.** Create a worktree from today's `HEAD`
  (for example via the harness worktree support) and use its own
  `cmake-build-<type>/`. The main tree is dirty and several of its tracked
  edits overlap this feature's files.
- **No concurrent builds.** Before running a build, check for an active
  build/build server (for example `pgrep -af 'ninja|make|cmake'` and inspect
  the relevant build directory). If one is active, wait or use a different
  build directory; never touch the user's `cmake-build-*`.
- **Shared-code steps require regression coverage.** Any step that touches
  `archx`, `instruction_stream`, or `tracing_tools` must rerun the existing
  `.is.xz` unit tests plus at least one trace regression before its review
  gate.
- **Preserve unrelated work.** Start every step with `git status`; never
  `git reset`, clean, or reformat files outside the step's scope.

### 4.2 Review subagent prompt template

Use a `general` subagent with a narrow, read-only brief:

```
Review step <N> of doc/development/microbench-grpc-plan.md.
Diff scope: <exact files>.
Read the step's acceptance criteria and AGENTS.md.

Check:
- correctness against the step's contract
- scope creep / unrelated changes
- repo conventions (naming, ownership, CMake registration)
- tests: are they meaningful, registered, and do they cover failure paths?
- generated/build artifacts accidentally included
- performance implications for the existing amperesys path

Do not run builds or tests. Do not edit, commit, or stage anything. Use
`git diff <base> -- <files>` as the review scope. Do not paste full files.
Return at most 30 lines:
1. verdict: PASS / PASS WITH NITS / BLOCK
2. blocking issues with file:line
3. non-blocking nits
4. missing tests or docs
```

### 4.3 Verification subagent prompt template

Use a `general` subagent for execution and log digestion:

```
In worktree <path>, run exactly:
  <commands>

Report only:
- exit status of each command
- failing test names (if any)
- first 20 lines of the first real error
- the measured numbers requested
Do not paste full logs, do not modify files, do not run other targets.
```

### 4.4 Menial-work delegation table

| Work | Delegate to | Main context receives |
|---|---|---|
| Locate symbols/files across the tree | `explore` | file:line list |
| Run targeted tests | `general` | pass/fail + failing names |
| Run `make check-format` | `general` | exit status + offending files |
| Collect latency measurements | `general` | table of p50/p95 per phase |
| Review a diff | `general` | verdict + issues |
| Inspect generated CMake/proto output | `general` | whether expected targets exist |
| Compare two traces / run `is_validate` | `general` | mismatch count + first mismatch |

## 5. Phased plan

Estimates are engineer-days for one person and exclude review latency. With
~25 review gates at 0.25–0.5 day each, budget an additional 6–12 days of
calendar time for reviews and fixes.

| Phase | Effort (days) |
|---|---:|
| 0 De-risk | 4–6 |
| 1 Fast trace production | 6–10 |
| 2 In-process front end (gated by Gate 1) | 10–14 |
| 3 gRPC service | 5–7 |
| 4 Hardening and delivery | 4–6 |
| **Subtotal** | **29–43** |
| Review/fix buffer | 6–12 |
| **Calendar total** | **35–55** |

If Gate 1 passes, phase 2 can be deferred, reducing the calendar total to
roughly 25–40 days.

### Phase 0 — De-risk (4–6 days)

#### Step 0.1 — Baseline latency breakdown

Status: **PARTIAL (2026-11-03)**. Simulation-path results are in section 9.
The QEMU rows were blocked then; Step 0.3 now provides a user-mode producer.

Goal: know where time goes before optimizing.

Work:
- Add a throwaway (not committed) script or use `panthera sim` to time:
  - QEMU system-mode with Linux boot;
  - QEMU system-mode bare-metal (if feasible);
  - QEMU user-mode on an existing static benchmark;
  - `amperesys` on a cached `.is`.
- Measure: process launch, boot, trace generation, `xz`, parse, simulation for
  10k / 100k / 1M instructions.
- Record numbers in this plan's "Measurements" section.

Acceptance: a table with median/min/max for the measured cases (3 reps where
practical); derived phases (image, end-of-elaboration, simulation) are point
estimates. A clear statement of whether trace generation or simulation
dominates at each size. **Met for the simulation/trace path: fixed ~1.7 s,
simulation ~9.5–11 kips, crossover ~20k instructions; QEMU rows were blocked
then and are now covered by Step 0.3.**

Subagent: execution subagent runs the timings; main agent records only the
table.

#### Step 0.2 — Raw-code driver spike

Status: **DONE (2026-11-03)**, pending user validation. Spike passes:
- core-level test: 203 instructions, 76 cycles, IPC ~2.67, from a
  6-instruction hand-assembled loop through `DriverCode` + perfect frontend +
  `SocForCorePV`;
- direct `DriverCode` test: the loop runs to the final `b .`, with `x1 == 100`
  and `x0 == 0`, proving the conditional branch and SUBS flags were emulated.

Goal: prove bytes → `Core` works at all.

Work:
- Hand-write a ~20-instruction loop (or assemble with an existing toolchain)
  and a minimal `DriverCode` prototype implementing `archx::Driver`.
- Instantiate the minimal SoC by setting `sys.soc_0.type SocForCorePV` (as in
  `src/ampere/config/model/sys/domino-core-rtl.cfg`) inside a small test
  executable; do not invent a direct-construction path.
- Set up a flat page table and an `ArchState` memory image.
- Read cycles/instructions from GPC.

Acceptance: the loop runs to completion and reports plausible cycles; the
prototype tells us exactly which `InstructionIS`/`emulate` assumptions break
(notably `program_counter_pa`, `memop_bytes`, `memop_predicated_off`).

**Deviations and findings:**
- The spike uses an identity VA→PA mapping and **no page table**; the loop has
  no memory operands. A real flat page table is deferred to Step 2.1.
- `Core` has no driver-injection point (`driver` and `construct` are private),
  so the spike adds a guarded `code:` prefix dispatch in `archx::makeDriver`.
  Existing `DriverIS` behavior is unchanged; this is the only production
  dispatch change.
- `program_counter_pa`, `physical_address`, `memop_bytes`, and
  `memop_predicated_off` all read stream metadata in `InstructionIS`. The
  spike uses a `CodeInstructionIS` subclass for the first three and a
  `memop_bytes()` that throws until Step 2.4 implements real access sizes.
- Wrong-path fetches now use negative inums, `commit()` enforces in-order
  identity, and `recover()` rewinds the fetch state and `last_fetched_id_`.
  Only the wrong-path fetch sign is covered by a direct test; `recover()` and
  out-of-order commit rejection are implemented but not yet exercised.
- Only the perfect frontend is supported: `next_pc()` returns the current PC
  and `fetch()` advances it. The BPU calls `next_pc()` several times ahead of
  `fetch()`, which this driver does not model.
- Known Phase 2 limitations: `recover()` throws if asked to rewind to an
  already-committed id (the core's RAW_SCRUB / alignment recovery may do this;
  `DriverIS` tolerates it), and a wrong-path fetch outside the image or to an
  unemulated opcode is fatal (`DriverIS` synthesizes a bogus instruction).
- `kestrel-user.cfg` does not load a `*-perfect-frontend.cfg`, so the PFE
  FIFO/credit settings must be supplied explicitly. `gpc.warmup_instructions=0`
  is required to make `max_instructions` effective without a
  `START_MEASUREMENT` control marker. The event-stream dummy must be enabled
  explicitly for `event_count`.

Files (worktree): `src/ampere/archx/driver_code.{h,cc}`,
`src/ampere/archx/test/test_driver_code_spike.cc`, guarded dispatch in
`src/ampere/archx/driver.cc`, CMake registration changes.

Build environment: the worktree needed the user's uncommitted Python 3.13/3.14
line in `CMakeLists.txt`, plus the main build's
`CMAKE_PREFIX_PATH=/tmp/opencode/immer-install`, Boost shim include, and the
`DRIVER_CODE_ABI_SHIM` workaround for the host's mismatched libsystemc
`SC_CPLUSPLUS`. These are environment adaptations, not feature changes.

Subagent: review subagent checks that the only production change is the
guarded `code:` factory dispatch plus new driver sources, with no behavior
change for existing paths; execution subagent builds/runs it in the isolated
worktree.

**Gate 0.A:** decide:
- whether the in-process path is viable for v1 or deferred to phase 2; and
- the stream-metadata strategy: synthesize an `is::Instruction` per fetch
  (preferred, reuses `InstructionIS` unchanged) versus extending
  `InstructionIS` with raw-code fields. Record the decision here; Step 2.4
  depends on it.

Spike outcome for Gate 0.A: in-process is viable for the supported subset.
Synthesizing `is::Instruction` cannot supply `memop_bytes` until after
emulation, so the practical choice is a **raw-code `InstructionIS` subclass or
explicit raw-code fields** (`program_counter_pa`, memop sizes), not a pure
stream-message synthesis. Decide the exact shape at the start of Phase 2.

#### Step 0.3 — User-mode QEMU + plugin feasibility

Status: **DONE for trace production (2026-11-04)**. The Ampere tracing-QEMU
8.0.0 user-mode binary was built from the fork's tarball, and the plugin now
produces a valid user-mode `.is.xz` from raw bytes.

Result:
- QEMU: `qemu-aarch64` 8.0.0 from
  `AmpereComputing/tracing-qemu@39724d94`, built with `--enable-plugins` for
  `aarch64-linux-user`.
- Input: a static AArch64 loop binary (`tiny_arm`).
- Triggers: `start-tracing-after-instructions=1000`,
  `stop-tracing-after-measuring-instructions=500`, `max-traces=1`.
- Plugin flags: `allow-missing-sysregs=true,flat-map=true`.
- Output: `mem_0.is.xz` (~2 KiB; `is_info`: 601 instructions = 500 measurement
  + 101 cooldown, 112 memops, 54 memory events, 3 synthetic TCR/TTBR sysreg
  events); `is_count`
  parses a realistic mix (ADD/SUBS/LDR/branches/etc.).

Plugin changes required:
- `allow-missing-sysregs`: user-mode QEMU's GDB XML lacks system registers
  such as `CNTKCTL_EL1`; the tracer now warns and skips them instead of
  exiting.
- `flat-map` for `.is`: identity VA=PA mapping. The tracer skips sysreg-based
  translation, sets instruction/memop PAs to their VAs, reads memory values
  through QEMU's virtual-memory path (54 memory events in the spike), and
  omits process-specific sysreg writes so identical runs are byte-identical.
  This is also the mapping the microbench server uses.
- `INSTRUCTION_TRACER_SC_API_SHIM`: opt-in SystemC API-version stub for hosts
  whose `libsystemc` was built with a different `SC_CPLUSPLUS` (the plugin is
  loaded into QEMU, which has no SystemC ABI symbol).

Trigger limitations:
- Use instruction-count, hint, or PC triggers. Perf-semihosting and most
  system-register access are system-mode only.
- `profile-comm`/`profile-pid` work, but a bare static binary can use the
  instruction triggers directly.

Resolved in Step 2.1: the plugin writes a synthetic `TCR_EL1`/`TTBR0_EL1` and
the trace source emits a flat `.mem` page table. `amperesys` now consumes the
trace (1,000 instructions, 1,583 cycles, IPC 1.183).

Build recipe (host fallback, no Nix):
- Download the tarball at the rev above; add the `ui/keycodemapdb` and
  `tests/fp/berkeley-{testfloat,softfloat}-3` submodules (the tarball omits
  them).
- Use `uv venv --python 3.11` plus `meson==0.63.3` and `ninja`.
- Configure: `--target-list=aarch64-linux-user --enable-plugins --disable-docs
  --disable-tools --disable-guest-agent --disable-capstone --disable-slirp
  --disable-werror --with-git-submodules=ignore`.
- Host patch: QEMU 8.0's `linux-user/syscall.c` redefines `struct sched_attr`;
  include `<linux/sched/types.h>` instead.
- `ninja -C build`; only `qemu-aarch64` needs to link. The separate pc-bios
  keymap generation can fail on a host without the Arabic xkb data and does not
  affect user-mode.

Gate 0.B: default QEMU mode is **user-mode with instruction-count triggers**
and `flat-map=true,allow-missing-sysregs=true`. System-mode bare-metal remains
the fallback for traces that need complete sysregs/page tables.

Goal: confirm no-boot QEMU tracing works with the existing plugin.

Work:
- Run `qemu-aarch64` on a static AArch64 benchmark with
  `libinstruction_tracer.so`, `-one-insn-per-tb`/singlestep.
- Determine which trigger modes work without semihosting (instruction count,
  PC, per-process exit).
- Produce a valid `.is` consumable by `amperesys`; compare against the
  system-mode trace for the same region.

Acceptance: a user-mode trace that runs through `amperesys`; documented trigger
limitations. **Met for trace production; `amperesys` still needs a flat
page-table/TCR setup.**

Subagent: execution subagent runs the comparisons; review subagent checks that
the plugin changes are scoped and system-mode behavior is unchanged.

#### Step 0.4 — gRPC/Nix/CMake build spike

Status: **DONE (2026-11-04)** with the sidecar fallback. No C++ gRPC toolchain
(`grpc_cpp_plugin`, `grpc++` CMake package) and no Nix environment are
available on this host. Python `grpcio` 1.84 and `protoc` 36.1 are available.

Result: a protobuf-defined `Run`/`RunBatch`/`Health` service round-trips over a
Unix-domain gRPC socket. The `Run` handler currently echoes the requested
instruction-word count; the real simulator runner is Step 3.2. Self-test:
`python3 microbench_server.py --self-test`. Separate client/server mode also
passes. `RunBatch` is implemented server-side; the client flag is deferred.

Decision (Gate 0.C): **v1 uses the Python sidecar.** The protocol is
transport-neutral, so the planned C++ server can replace the sidecar when the
Nix environment provides gRPC. The simulation dominates latency, so the Python
transport overhead is not material.

Files: `src/ampere/microbench/proto/microbench.proto`,
`src/ampere/microbench/python/{microbench_server.py,microbench_client.py,README.md,.gitignore}`.
Generated stubs under `gen/` are not checked in.

Goal: prove the dependency stack links before the server work starts.

Work:
- Add `grpc` (and its transitive abseil/re2/c-ares) to the Nix environment
  behind a temporary flag or branch.
- Build a minimal C++ server/client pair using `find_package(gRPC CONFIG)` and
  the repo's pinned protobuf (`protobuf_21`).
- Check for duplicate protobuf runtime symbols and module-vs-config CMake
  conflicts.
- If the conflict is not resolvable cheaply, evaluate one shared protobuf
  version, a Python gRPC sidecar, or a non-gRPC transport.

Acceptance: a minimal `Run`-shaped RPC round-trips in the Nix environment, or a
documented decision to use the sidecar. **Met via the sidecar.**

Subagent: execution subagent runs the build; review subagent checks that the
spike is isolated and does not change the default C++ build.

**Gate 0.C:** dependency stack approved, or sidecar/alternative transport
chosen. **Resolved: sidecar chosen for v1.**

---

### Phase 1 — Fast trace production (6–10 days)

#### Step 1.1 — Uncompressed, fd-capable instruction streams

Status: **DONE (2026-11-04)**. Added `InstructionStreamReader(fd)` and
`InstructionStreamWriter(fd)`. Ownership is now explicit:

- fd constructors hand the descriptor to `FileInputStream`/`FileOutputStream`,
  which close it.
- path constructors `dup(fileno(fp))` for the protobuf stream and close the
  original `FILE*` with `pclose` for pipes or `fclose` for plain files.

This fixes two pre-existing issues: the reader destructor's `pclose` plus
`FileInputStream::Close()` double close, and calling `pclose` on plain `fopen`
streams. Uncompressed `.is` was already selected by filename extension and is
covered by the existing `test.is` fixture.

A new pipe round-trip test writes START + two instructions + STOP, reads them
back through the fd reader, confirms EOF, and verifies both descriptors are
closed after the reader/writer are destroyed. All 8
`instruction_stream_unit` and 14 `event_stream_unit` tests pass.

Work:
- Add an `InstructionStreamReader` constructor/overload that accepts an fd or
  stream, mirroring `ProtobufStreamReader(int fd, ...)`.
- Make trace output selectable as `.is` (uncompressed) vs `.is.xz`.
- Keep the compressed path for archival traces.

Acceptance: unit test reads a stream from a pipe; existing `.is.xz` tests pass
unchanged. **Met.**

Files: `src/ampere/instruction_stream/instruction_stream.{h,cc}`,
`src/ampere/protobuf_utils/protobuf_stream.{h,cc}`,
`src/ampere/instruction_stream/test/test_is_reader.cc`.

Subagent: review subagent checks API consistency and that no compression
behavior changed.

#### Step 1.2 — Trace and result cache

Work:
- Content hash over code, data sections, entry, caps, the **resolved** config
  (not just the config name), trigger window, QEMU/plugin version, and the
  simulator binary revision. Include a schema/version salt.
- Store `.is` + a precomputed memory image under a configurable cache root
  (tmpfs-friendly). Do not rely on the O(trace) "build from instruction
  stream" fallback (≈50 ms at 48k instructions, much larger for long traces);
  generate `.mem`/`.elf` once per code blob.
- **Also cache the final `RunResponse`** keyed by the same hash. Given the
  measured ~10–14 kips, a result cache is the only way repeated requests are
  fast. Invalidate on any simulator/config/trace-source version change.
- LRU eviction with size cap; atomic writes (temp + rename).
- Expose hit/miss/generation metrics, separately for trace and result cache.

Acceptance: the second identical request returns from the result cache without
invoking QEMU or simulation; a trace-cache hit skips QEMU but still simulates;
corrupt/truncated entries are detected and regenerated; concurrent writers are
safe.

Files: new `src/ampere/microbench/trace_cache.{h,cc}` + tests.

Subagent: review subagent focuses on hashing completeness, collision handling,
and filesystem races.

#### Step 1.3 — User-mode QEMU trace source

Status: **DONE (2026-11-04)**. Implemented in the Python sidecar as
`qemu_trace_source.py`; the C++ path is deferred until the runner exists.

Result:
- `Run` wraps the submitted words in a static AArch64 harness ELF, places the
  code at `0x400100`, runs tracing-QEMU 8.0 with the patched plugin, and
  returns the `.is.xz` plus `is_info` metadata.
- Harness: `_start` at `0x400000`, fixed BSS stack, zeroed `x0`–`x29`, `bl` to
  the code, exit syscall; `--build-id=none` avoids a linker note overlapping
  the `.org` padding.
- Triggers: `start-tracing-after-pc=0x400100`,
  `stop-tracing-after-measuring-instructions=N`, `cooldown-instructions=100`,
  `max-traces=1`, `allow-missing-sysregs=true,flat-map=true`.
- The source monitors the output and terminates QEMU once the trace is
  finalized. The tracer removes `<trace>.tmp` after the final stream is
  complete; `max-traces=1` stops tracing but the source terminates a
  self-looping QEMU itself.
- Determinism: two identical runs produce byte-identical `.is.xz` and `.mem`
  (SHA-256 verified). Flat traces omit process-specific sysreg writes; they
  emit a fixed synthetic TCR/TTBR (3 sysreg events), so identical runs remain
  byte-identical.
- Completion is detected by the tracer removing `<trace>.tmp` after the final
  stream is copied and finalized, then validated with `is_info`; the
  `Stopping tracing` log line alone is printed too early. A truncated or short
  trace is rejected instead of returned as success.
- Latency (3 reps; includes harness assembly, QEMU startup, trace
  finalization, and `is_info` validation):

  | Instructions | Median | Min | Max |
  |---:|---:|---:|---:|
  | 10k | 0.373 s | 0.352 s | 0.428 s |
  | 100k | 2.996 s | 2.994 s | 3.016 s |
  | 1 instruction (self-test) | 0.077 s | — | — |

  For comparison, simulation alone is ~1.15 s at 10k and ~8–10 s at 100k, so
  trace generation is not the dominant cold-path cost at these sizes. The
  numbers include harness assembly, QEMU startup, trace finalization, and
  `is_info` validation.

Limitations:
- `max_instructions` is required; `max_cycles` alone is not supported.
- `entry_pc` must be 0 or `0x400100`; the code base is fixed.
- Data sections are rejected; `.data`/`.bss` support is a follow-up.
- `cycles`/`ipc` are 0 until the Step 3.2 runner.
- The trace includes a flat TCR/TTBR and `.mem`; `amperesys` consumes it
  (Step 2.1). Addresses outside the low 1 GiB fault deterministically.
- No sandbox: user-mode QEMU executes client code with host syscalls. The
  sidecar binds a Unix socket by default; `--allow-remote` additionally
  requires `--allow-untrusted`.

Hardening:
- Code is capped at 4 MiB (gRPC's receive limit), `max_instructions` at 1M,
  and concurrent traces at 4 (`--max-concurrent`); excess requests get
  `RESOURCE_EXHAUSTED` rather than queueing.
- `is_info` is required for validation and metadata; `PANTHERA_IS_INFO` is
  honored.
- Tests: `test_qemu_trace_source.py` covers harness generation, metadata
  parsing, and missing-tool errors; the QEMU integration test runs when
  `PANTHERA_QEMU_AARCH64`, `PANTHERA_TRACER_PLUGIN`, and `PANTHERA_IS_INFO` are
  set.

Work:
- Wrap the benchmark bytes in a statically linked bare-metal ELF (or a small
  harness ELF with a loadable code blob). Build non-PIE (`-no-pie`) and avoid
  ASLR-dependent addresses so traces are reproducible. **Done.**
- Launch `qemu-aarch64` with the plugin; no kernel/rootfs. **Done.**
- Add the minimal plugin trigger mode chosen in Gate 0.B if existing modes are
  insufficient. **Not needed; PC/count triggers suffice.**
- Capture `.is` and the memory image; return a `TraceResult`. **Done for
  `.is`; data sections pending.**

Acceptance: end-to-end trace from raw bytes; same input produces byte-identical
output across runs; failure paths (plugin missing, QEMU crash, timeout) return
structured errors. **Met for code-only traces and determinism; data sections
remain a follow-up.**

Files: `src/ampere/microbench/python/qemu_trace_source.py`,
`src/ampere/microbench/python/microbench_server.py`,
`src/ampere/microbench/python/microbench_client.py`,
`src/ampere/microbench/python/test_qemu_trace_source.py`,
`src/ampere/microbench/python/README.md`, and
`src/ampere/tracing_tools/is_tracer.h` (flat sysreg omission).

Subagent: review subagent checks process hygiene (no zombies, no leaked fds),
timeout handling, determinism, and that plugin changes are backward
compatible.

#### Step 1.4 — Bare-metal system-mode fallback

Work:
- Only if Gate 0.B requires it: a tiny `_start` that sets up EL1, runs the
  code, and exits via semihosting; launch with
  `qemu-system-aarch64 -M virt -bios none -kernel ... -semihosting`.
- Reuse the same `TraceSource` interface.

Acceptance: feature parity with Step 1.3 for the supported benchmark subset.

Subagent: review subagent checks that no Linux/rootfs dependency remains.

#### Step 1.5 — Trace-worker pool and process supervision

Scope: trace workers only. Simulation/server lifecycle is Step 3.4; do not
build a second pool here.

Work:
- Fixed-size pool of trace workers; bounded queue; backpressure.
- Timeouts and kill-on-hang; crash isolation; restart policy.
- Structured worker logs to files, not stdout.

Acceptance: N concurrent requests scale; a deliberately crashing worker is
replaced without affecting in-flight requests.

Subagent: execution subagent runs a stress test; review subagent checks fd and
process lifecycle.

#### Step 1.6 — (Candidate) fork-after-elaboration worker

Status: candidate, not included in the Phase 1 estimate. Only start if Gate 1
shows the ~1.7 s fixed cost is still a problem after caching.

Goal: remove the ~1.44 s construct + ~0.21 s end-of-elaboration floor
(~1.65 s total). The image build is already cached in Step 1.2 and is small,
so do not count it here.

Work:
- Elaborate `Sys` once in a parent, then fork per request and run `sc_start` in
  the child; serialize counters back over a pipe.
- Spike SystemC 2.3.3 fork-safety after elaboration first. Do not assume it is
  safe.
- If unsafe, fall back to re-elaboration per request (current design).

Acceptance: a forked child produces the same metrics as a fresh process for a
known trace, and per-request fixed cost drops below ~200 ms (from ~1.7 s).
Note that a 10k benchmark still takes ~1.15 s of simulation; fork does not
make it sub-second on its own.

Subagent: execution subagent runs the fork/A-B comparison; review subagent
checks state isolation and zombie/fd handling.

**Gate 1:** the revised targets in section 1 are met: result-cache hit
<= 5 ms, simulate path within the stated times, and QEMU absent on a hit. The
cold QEMU number can now be measured with the Step 0.3 user-mode producer. Until
`SimulationRunner` exists (Step 3.2), measure through a temporary `amperesys`
subprocess shim and label the numbers as shim-only. Do not finalize the phase-2
deferral until at least one cold QEMU request has been measured. If the targets
are met, phase 2 can be deferred.

---

### Phase 2 — In-process raw-code front end (10–14 days, gated by Gate 1)

Only do this if measured latency/throughput still misses the objective.

#### Step 2.1 — Memory image and flat page table

Status: **DONE (2026-11-04)** for the v1 sidecar path. The trace source emits a
flat page table and the plugin emits synthetic TCR/TTBR; `amperesys` now runs
the user-mode trace.

Result:
- Plugin flat mode writes `TCR_EL1 = 25 | (2 << 32)` (4 KiB granule, 39-bit
  VA, IPS=2), `TTBR0_EL1 = 0x500000`, and `TTBR1_EL1 = 0`.
- The trace source writes `<trace>_0.mem` with a 4 KiB L1 table at `0x500000`:
  entry 0 is a level-1 1 GiB block descriptor (`0x401`, AF=1) mapping VA
  0–1 GiB to PA 0–1 GiB; other entries are invalid.
- `amperesys -cfg siryn.cfg -instruction_stream_name <trace>` loads the `.mem`
  and runs the trace: **1,000 instructions, 1,583 cycles, IPC 1.183**.
- Determinism: the `.is.xz` and `.mem` are byte-identical across identical
  runs.
- The mapping covers the code (`0x400000`–`0x400100`) and stack; addresses
  outside the low 1 GiB fault deterministically.

Work:
- Build a sparse `ArchState` from request sections. **Deferred: the `.mem`
  currently contains the page table only; data sections remain a follow-up.**
- Generate a flat mapping (1 GB blocks or 2 MB pages) covering code, data,
  stack, and a scratch arena. **Done for the low 1 GiB.**
- Deterministic fill policy for uninitialized memory. **Page table is
  zero-filled; user data/scratch sections remain.**
- Optional MMU-off/identity fast path. **Not needed.**

Acceptance: arbitrary benchmark addresses in the mapped range translate;
out-of-range accesses fault deterministically; unit tests for the block/page
descriptor. **Met for the low 1 GiB; the `.mem` header and block descriptor
are unit-tested.**

Files: `src/ampere/tracing_tools/is_tracer.h`,
`src/ampere/microbench/python/qemu_trace_source.py`,
`src/ampere/microbench/python/test_qemu_trace_source.py`. The C++
`archx/code_image` path is deferred while the v1 runner is the sidecar.

#### Step 2.2 — `DriverCode` core

Work:
- Implement `archx::Driver`: `next_pc`, `fetch`, `commit`, `recover`,
  `read_memory`, `read_sysreg`, `exception_level`, `asid`, `committed_id`.
- Fetch 4 bytes at the current PC, translate VA→PA, build an
  `is::Instruction` with `program_counter{va,pa}` and `opcode`.
- Run `emulate_correct_path`; populate memops (va/pa/bytes) from emulated
  addresses and encoding-derived sizes.
- Commit applies `arch_state_output` to functional state.
- Model as `src/ampere/archx/driver_code.{h,cc}` (driver) plus a thin
  `microbench` adapter.

Acceptance: runs straight-line integer, branch, and load/store loops; produces
the same dynamic PC/opcode sequence as a QEMU trace for the same code.

Subagent: review subagent focuses on the Driver contract and state updates;
execution subagent runs differential tests.

#### Step 2.3 — Branch and recovery model

Work:
- Direct branches: use `InstructionIS` target computation.
- Indirect branches/returns: emulated target from register state.
- `recover()`: checkpointed branch history so mispredict recovery re-fetches
  the correct path.
- Wrong-path fetch reads from the code image; mark artificial instructions.

Acceptance: branch-heavy microbenchmarks match QEMU trace control flow;
mispredict recovery does not leak state.

#### Step 2.4 — Memop metadata

Work:
- Implement the Gate 0.A decision. If synthesizing `is::Instruction`:
  populate `program_counter{va,pa}`, `opcode`, and
  `memop{va,pa,bytes,predicated_off}` per fetch. If extending
  `InstructionIS`: add explicit raw-code fields and update
  `program_counter_pa()`, `num_addresses()`, `memop_bytes()`, and
  `memop_predicated_off()` together.
- Derive access size/bytes for emulated instructions (extend `emulate` to
  record sizes or add a small encoding→size helper).
- Note: `InstructionIS` currently reads these from its embedded
  `is::Instruction` (`src/ampere/archx/instruction_is.cc:119,131,141,154`), so
  neither option is a no-op.

Acceptance: load/store microbenchmarks issue correct byte counts and addresses;
`is_validate`-style checks pass on the synthesized stream.

#### Step 2.5 — Halt convention, caps, and faults

Work:
- Halt on a magic encoding or a configured sentinel PC.
- Mandatory `max_instructions` and `max_cycles` backstops.
- Policy for translation faults, illegal encodings, and exceptions
  (stop with structured error vs emulate a trap).
- Optional dump of the synthesized `.is` for debugging.

Acceptance: runaway code always terminates; bad input returns a typed error;
no process crash from arbitrary bytes.

#### Step 2.6 — Differential validation against QEMU

Work:
- For a corpus of benchmarks, compare:
  - committed PC/opcode sequences;
  - memory addresses/sizes;
  - final architectural state where observable;
  - cycle counts within an agreed tolerance (they should match exactly if the
    trace is identical).
- Use the existing `validate_emulation` infrastructure where possible.

Acceptance: agreed corpus passes; every mismatch has a documented root cause or
is excluded with a reason.

**Gate 2:** in-process trace generation meets the section 1 latency targets
(measured through the Step 3.2 runner if it exists; otherwise the shim) and
passes the differential corpus. Otherwise keep QEMU as the producer.

---

### Phase 3 — gRPC service (5–7 days)

#### Step 3.1 — Proto and generated targets

Work:
- Define `microbench.proto`: `Run`, `RunBatch`, `Health`, optional
  `ListConfigs`.
- Keep messages small; use `bytes` for code/data; define typed status errors.
- For the sidecar, generate Python message stubs with `protoc --python_out`;
  no gRPC plugin is needed because the service is registered with generic
  handlers. If the C++ server is revived, add CMake gRPC generation targets
  and the Nix `grpc` dependency at that point.

Acceptance: proto compiles, generated code is not checked in, a stub server
starts and answers `Health`.

Files: `src/ampere/microbench/proto/microbench.proto`,
`src/ampere/microbench/CMakeLists.txt`.

Subagent: review subagent checks CMake target hygiene and protobuf version
compatibility (the repo pins `protobuf_21`).

#### Step 3.2 — Runner library

Work:
- Extract the `sc_main`-equivalent setup into a callable `SimulationRunner`:
  fresh `sc_simcontext`, `Environment`, `Clock`, `Sys`, event collector.
- In-process results via an `EventStreamDummy`-style collector; no `evp`, no
  files.
- Return cycles, instructions, IPC, PMU map, wall time, and logs.

Acceptance: a runner unit test reproduces `amperesys` metrics for a known
trace/config; two sequential runs in one process are independent.

Files: `src/ampere/microbench/runner.{h,cc}` + tests.

#### Step 3.3 — RPC handlers

Work:
- Validate request (caps present, sections in range, config resolvable).
- Route to `TraceSource` then `SimulationRunner`.
- `RunBatch` to amortize startup; stable ordering of results.
- Timeouts and cancellation policy: serialized in-process runs cannot be
  cancelled safely; document that cancellation kills the worker.
- Structured error mapping (invalid argument, resource exhausted, internal,
  deadline exceeded).

Acceptance: end-to-end `Run` and `RunBatch`; a hung worker is killed and the
RPC returns `DeadlineExceeded`.

#### Step 3.4 — Server lifecycle and concurrency

Work:
- One simulation at a time per worker process; pool for parallelism.
- Bounded queue and admission control; reject rather than OOM.
- Graceful shutdown; drain in-flight requests; reap workers.
- Optional Unix-domain socket for local low-latency clients.

Acceptance: stress test with mixed sizes; no leaks; clean shutdown.

Subagent: execution subagent runs the stress test; review subagent checks
signal/exit handling.

---

### Phase 4 — Hardening and delivery (4–6 days)

#### Step 4.1 — Test suite

- Unit: trace cache, hashing, page tables, driver decode/commit/recover,
  proto validation, error mapping.
- Integration: end-to-end `Run` for each supported benchmark class.
- Differential: in-process vs QEMU traces.
- Negative: illegal opcode, fault, runaway loop, oversized request, truncated
  stream.
- Performance: latency budget assertions (cache hit, cache miss, batch).
- Register everything with `add_cpp_test` / `add_python_test`; use the
  Makefile selectors' labels correctly (`integration`, `benchmark`, `veryslow`).

#### Step 4.2 — Documentation

- User-facing page: how to start the server, request schema, limits,
  determinism, supported ISA subset.
- Developer page: architecture, trace-source extension points, worker model.
- Update `mkdocs.yml` only for pages that should be published.
- Update `doc/development/testing.md` if the test workflow changes.

#### Step 4.3 — Packaging and CI

- Install the server binary and proto descriptors.
- Add a smoke test to CI that starts the server and runs one benchmark.
- Keep the existing `amperesys` flow untouched.

#### Step 4.4 — Performance sign-off

- Record p50/p95/p99 latency and throughput for 10k/100k/1M instructions.
- Record cache hit rate on a realistic batch.
- State `Expected Perf Drift: <value or negligible>` in the review; the
  existing simulator path should be unchanged.

## 6. File map (expected)

```
src/ampere/microbench/
  proto/microbench.proto
  python/                    # v1 gRPC sidecar (Step 0.4)
    microbench_server.py
    microbench_client.py
  CMakeLists.txt             # if/when the C++ server is revived
  server.cc                  # C++ gRPC server (deferred)
  runner.{h,cc}
  trace_cache.{h,cc}
  trace_source.h
  qemu_trace_source.{h,cc}
  inprocess_trace_source.{h,cc}
  qemu/                      # bare-metal harness / ELF builder
  test/
src/ampere/archx/
  driver_code.{h,cc}         # Step 0.2 spike; hardened in phase 2
  code_image.{h,cc}          # phase 2
src/ampere/instruction_stream/
  instruction_stream.{h,cc}  # fd reader, uncompressed option
src/ampere/tracing_tools/
  instruction_tracer.cc      # trigger/fd options, if needed
  is_tracer.h
```

The sidecar does not link the C++ model. Step 3.2 decides between a persistent
worker process (framed stdin/stdout or Unix socket) and one subprocess per
request; prefer a persistent worker.

Register the new subdirectory in `src/ampere/CMakeLists.txt`; register tests in
the owning module's `CMakeLists.txt`. Do not add global include paths or
flags.

New C++ and proto files need the standard SPDX header
(`SPDX-License-Identifier: BSD-3-Clause`). Proto files are not covered by
clang-format; keep them consistent manually.

## 7. Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Emulator coverage gaps (SVE/SME, atomics, FP) | Benchmarks fail or diverge | Define supported subset; differential corpus; add handlers incrementally |
| `InstructionIS` stream metadata assumptions | Crashes/asserts on raw path | Synthesize `is::Instruction`; unit-test `program_counter_pa`/`memop_bytes` |
| gRPC vs pinned `protobuf_21` | Build/link failure, duplicate runtime | De-risk in Step 0.4; prefer one protobuf for both; sidecar fallback |
| SystemC global state / no reset | Cross-request contamination | Fresh simcontext + `Sys` per request; process-per-worker |
| QEMU plugin triggers in user mode | No trace start/stop | Gate 0.3; add minimal plugin mode; bare-metal fallback |
| Trace/result cache poisoning | Wrong results | Versioned hash, atomic writes, integrity check, disable knob |
| Stale result cache | Wrong metrics | Version + resolved-config salt; explicit invalidation |
| Slow stream-exhaustion halt | Tiny benchmarks take seconds | Explicit halt marker + mandatory caps |
| Runaway/untrusted code | Worker hang/crash | Mandatory caps, timeouts, kill/replace worker |
| Host compromise via client code | Arbitrary host syscalls in user-mode QEMU | Security boundary section 3.1; sandbox; loopback/Unix socket |
| Non-determinism (ASLR, versions) | Cache mismatches, irreproducible results | Non-PIE harness, version + resolved-config salt, seeded fill |
| Dirty worktree / concurrent build | User work lost, flaky builds | `git status` first; isolated worktree/build dir; no global clean |

## 8. Open questions

1. Which benchmark families must v1 support: integer, scalar FP, SIMD/NEON,
   SVE, atomics?
2. Is EL0 vs EL1 modeling important, or is a flat EL0-ish environment enough?
3. How is initial data supplied: sections, generated patterns, or fixed arenas?
4. Do clients need per-instruction traces/statistics, or only aggregates?
5. Confirm the revised targets in section 1 and the expected request rate (used
   to size the worker pool).
6. Confirm the v1 security boundary: local-only vs network, and which sandbox
   (seccomp, namespace, container) is available in the deployment environment.
7. Should `RunBatch` share a config and memory image, or allow per-item
   overrides?
8. Is byte-identical determinism across machines required, or only across runs
   on the same host?

## 9. Measurements (Step 0.1 results, 2026-11-03)

Host: the current development machine. Binary: `install/bin/amperesys`,
`(1fa217bb1ba7d68a806bf0d0584a8779204a5fe0, refs/heads/main, DIRTY)`.
All times are wall seconds.

Method: throwaway scripts `/tmp/opencode/microbench-step01/measure*.py`. Two
runs:

- **Small-trace run** (`domino.cfg` + `dhrystone`/`ls_0`/`gap`), representative
  of a microbenchmark-sized trace.
- **Long-trace run** (`siryn.cfg` + `polybench_2mm_0`, 3.1M instructions) with
  `max_instructions` caps. **The image build in this run is an artifact**: it
  walks the whole 3.1M-instruction trace regardless of the cap. Do not read it
  as a per-request microbench cost.

`dump_clocks` exits with status 1 by design.

### Small-trace results (representative of a microbench)

| Case | Launch | Construct | End-of-elab | Image build | Simulation | Total (median) |
|---|---:|---:|---:|---:|---:|---:|
| `amperesys --version` | 0.013 | n/a | n/a | n/a | n/a | 0.013 |
| construct + `dump_clocks` (exits before end-of-elab) | 0.013 | 1.437 | — | not built | n/a | 1.450 |
| 1k probe, image ON | 0.013 | 1.437 | 0.21 | 0.048 | ~0.09 | 1.797 |
| 1k probe, image OFF | 0.013 | 1.437 | 0.21 | — | ~0.09 | 1.749 |
| dhrystone 10k (11,002 parsed) | 0.013 | 1.437 | 0.21 | 0.048 | ~1.15 | 2.863 |
| dhrystone 30k (31,001 parsed) | 0.013 | 1.437 | 0.21 | 0.048 | ~2.89 | 4.602 |
| ls_0 full (7,636 parsed) | 0.013 | 1.437 | 0.21 | ~0.01 | ~0.99 | 2.660 |
| gap-pr-livej full (1,806 parsed) | 0.013 | 1.437 | 0.21 | ~0.01 | ~4.26 incl. exhaustion tail | 5.931 |

The 0.21 s end-of-elaboration column is derived, not directly measured: the
1k probes execute 1,000 instructions (~0.09 s at ~11 kips), leaving
1.797 − 0.013 − 1.437 − 0.048 − 0.09 = 0.209; image OFF:
1.749 − 0.013 − 1.437 − 0.09 = 0.209. `dump_clocks` exits before this point,
which is why its row has no end-of-elab entry. Rows reconcile within ~30 ms.

Min/max (s), 3 reps: construct 1.401–1.455; 10k 2.819–2.919;
30k 4.554–4.634; ls_0 2.594–2.688; gap 5.913–6.012.

Effective simulation-only rate: ~9.5 kips at 10k, ~10.7 kips at 30k; marginal
rate from 10k to 30k is ~11.5 kips; long runs are 10.6–13.6 kips.

### Long-trace results (for scale; image build is an artifact)

| Case | Launch | Construct | Image build | Simulation | Total (median) |
|---|---:|---:|---:|---:|---:|
| 100k cap on 3.1M trace | 0.013 | 1.44 | 1.85 | ~8.0 | 11.31 |
| 1M cap on 3.1M trace | 0.013 | 1.44 | 1.85 | ~73.6 | 76.87 |
| existing full run (3.1M) | — | — | — | — | 305–325 s wall (297–301 CPU), ~10.1–10.6 kips |
| `qemu-aarch64` tiny static binary, no plugin | 0.023 | n/a | n/a | n/a | 0.023 |
| system-mode + Linux | blocked | blocked | blocked | blocked | blocked |

Step 0.3 addendum: user-mode QEMU 8.0 + the patched plugin produced a
601-instruction `.is.xz` (500 measurement, 101 cooldown; 112 memops, 54 memory
events, 3 synthetic TCR/TTBR sysreg events) from a static `tiny_arm` loop.
The trace source also writes a `.mem` page table; `amperesys` now consumes the
trace (Step 2.1).

### Step 0.1 findings

- **Fixed cost for a microbench-sized trace is ~1.7 s**: 13 ms launch +
  1.44 s construct + ~0.21 s end-of-elaboration/first-instruction setup +
  ~50 ms image build at 48k instructions. The earlier ~3.2 s figure was an
  artifact of charging a 3.1M-instruction image build to a short run.
- **Simulation dominates above ~20k instructions.** Rates are ~9.5 kips at
  10k, ~10.7 kips at 30k, marginal ~11.5 kips, and 10.6–13.6 kips on long
  runs. 30k ≈ 2.9 s, 100k ≈ 9–10 s, 1M ≈ 74 s of pure simulation. At 10k the
  ~1.7 s fixed cost is still larger than the ~1.15 s of simulation.
- **Stream exhaustion is a slow halt.** The 1,806-instruction gap trace took
  5.9 s, most of it in the exhaustion/drain tail. Generated microbenchmarks
  must end with an explicit halt marker (or a mandatory instruction cap), not
  by running off the end of the stream.
- **Trace caching alone does not make repeated requests fast.** Add a
  **result cache** (Step 1.2).
- **The construct + setup floor is the main fixed cost.** Elaborating once and
  forking per request could remove ~1.65 s (construct + end-of-elaboration) if
  SystemC 2.3.3 is fork-safe after elaboration (Step 1.6 candidate). Simulation
  remains ~1.1 s at 10k, so fork alone does not make a 10k run sub-second.
- **Precompute the memory image** once per code blob. The ~50 ms at 48k
  instructions is small but avoidable; the existing tracer already supports
  `trace-elf=true` as a cheaper image source than building from the stream.
- **QEMU trace generation was not measured.** The installed plugin requires
  the Ampere QEMU 8.0 API (`qemu_plugin_read_gdb_xml` and others; the system
  QEMU 11.1.3 exports none of it), and no kernel image is available here.
  This is a prerequisite for Steps 0.3 and 1.3.

### Blocked measurements

- QEMU system-mode + Linux: needs the patched kernel image and rootfs.
- QEMU user-mode trace generation: **resolved in Step 0.3** with the Ampere
  QEMU 8.0 build and the patched plugin.
- Cold-request total (QEMU + simulation): can now be measured once the runner
  exists.

## 10. Definition of done

- Server builds and installs with the supported Nix/CMake flow.
- `Run` and `RunBatch` return correct, deterministic metrics for the supported
  benchmark subset.
- A result-cache hit returns metrics identical to a cold run for the same
  request.
- Cache hit path does not invoke QEMU.
- Worker crashes and timeouts do not take down the server.
- Unit, integration, differential, and negative tests are registered and pass.
- Documentation exists and `make doc` passes.
- The security boundary from section 3.1 is enforced and documented.
- Existing `.is.xz` tests and at least one trace regression still pass.
- `make check-format`, `make python-lint` (if Python changed), and the relevant
  test targets pass.
- Review records `Expected Perf Drift: None (existing path unchanged)`.
