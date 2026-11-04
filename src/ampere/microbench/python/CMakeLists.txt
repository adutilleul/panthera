# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause

# Tests need protoc for the message stubs and grpcio to run the server. The
# four unit modules skip their QEMU/amperesys cases, so registering them when
# these dependencies exist does not require the external toolchain.
find_program(MICROBENCH_PROTOC protoc)
execute_process(
    COMMAND ${Python3_EXECUTABLE} -c "import grpc"
    RESULT_VARIABLE MICROBENCH_GRPC_STATUS
    OUTPUT_QUIET
    ERROR_QUIET)

if(MICROBENCH_PROTOC AND MICROBENCH_GRPC_STATUS EQUAL 0)
    add_python_test(microbench_sidecar_python_test
        DISCOVER ${CMAKE_CURRENT_SOURCE_DIR}
        LABELS integration
        TIMEOUT 1200)

    # Optional end-to-end check for CI: it starts the server, runs a benchmark
    # twice, checks Stats, and verifies shutdown. It skips without the external
    # tool environment; CI can pass --require to make a missing environment
    # fail.
    add_test(NAME microbench_sidecar_smoke
        COMMAND ${Python3_EXECUTABLE} ${CMAKE_CURRENT_SOURCE_DIR}/smoke_test.py)
    set_property(TEST microbench_sidecar_smoke PROPERTY LABELS integration)
    set_property(TEST microbench_sidecar_smoke PROPERTY TIMEOUT 300)
    # Generated stubs must not be written into a possibly read-only source tree.
    set_tests_properties(microbench_sidecar_python_test microbench_sidecar_smoke
        PROPERTIES ENVIRONMENT "PANTHERA_GEN_DIR=${CMAKE_CURRENT_BINARY_DIR}/gen")
else()
    message(STATUS "Skipping microbench sidecar tests (protoc/grpcio not found)")
endif()

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
