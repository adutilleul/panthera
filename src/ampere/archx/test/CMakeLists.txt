# Copyright (c) 2024 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause

set(TESTS
    test_addr.cc
    test_driver.cc
    test_driver_bs.cc
    test_instruction.cc
    test_decode.cc
    test_uop_info.cc
    test_arch_state.cc
    test_xlat_decode.cc
    test_prefetch_address.cc
    test_sysreg.cc)

add_cpp_test(archx_unit SOURCES ${TESTS})
target_link_libraries(archx_unit archx sc_main_stub)
target_compile_options(archx_unit PRIVATE -Wno-sign-compare)

# Step 0.2 spike: raw-code driver through SocForCorePV.
option(DRIVER_CODE_ABI_SHIM
       "Define the SystemC API-version stub when libsystemc uses a different SC_CPLUSPLUS" OFF)
add_cpp_test(driver_code_spike SYSTEMC SOURCES test_driver_code_spike.cc LABELS integration TIMEOUT 300)
target_link_libraries(driver_code_spike
                      uarch_common ampere_core ampere_mesh ampere_memory archx ampere_soc ampere_sc_testing)
target_compile_options(driver_code_spike PRIVATE -Wno-sign-compare)
if(DRIVER_CODE_ABI_SHIM)
    target_compile_definitions(driver_code_spike PRIVATE DRIVER_CODE_ABI_SHIM)
endif()
