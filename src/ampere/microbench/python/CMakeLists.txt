# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause

# Unit tests run without QEMU; the QEMU/amperesys integration tests skip unless
# the PANTHERA_* tool environment is set. Label them integration so
# `make unit-test` does not require the external toolchain.
add_python_test(microbench_sidecar_python_test
    DISCOVER ${CMAKE_CURRENT_SOURCE_DIR}
    LABELS integration
    TIMEOUT 1200)

install(FILES
    microbench_server.py
    microbench_client.py
    qemu_trace_source.py
    simulation_runner.py
    trace_cache.py
    process_registry.py
    DESTINATION lib/python/panthera/microbench/python)

install(FILES ${CMAKE_CURRENT_SOURCE_DIR}/../proto/microbench.proto
    DESTINATION lib/python/panthera/microbench/proto)

install(PROGRAMS microbench-server microbench-client DESTINATION bin)
