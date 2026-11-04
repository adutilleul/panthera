# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause

# The v1 sidecar is Python. Register its tests only when the optional runtime
# dependencies (protoc and grpcio) are available; the main build must not fail
# on hosts without them.
find_program(MICROBENCH_PROTOC protoc)
execute_process(
    COMMAND ${Python3_EXECUTABLE} -c "import grpc"
    RESULT_VARIABLE MICROBENCH_GRPC_STATUS
    OUTPUT_QUIET
    ERROR_QUIET)

if(MICROBENCH_PROTOC AND MICROBENCH_GRPC_STATUS EQUAL 0)
    add_subdirectory(python)
else()
    message(STATUS "Skipping microbench sidecar tests (protoc/grpcio not found)")
endif()
