//
// Copyright (c) 2018-2024 Ampere Computing. All rights reserved.
// SPDX-License-Identifier: BSD-3-Clause
//

#include "ampere/archx/driver.h"
#include "ampere/archx/driver_bs.h"
#include "ampere/archx/driver_code.h"
#include "ampere/archx/driver_is.h"

#include <string_view>

namespace ampere::archx {

std::unique_ptr<Driver> makeDriver(std::string filename,
                                   uint64_t skip_instructions,
                                   bool speculative_memory_image,
                                   bool use_wrong_path_emulation,
                                   std::uint64_t validate_emulation,
                                   std::string mem_image_path)
{
    // Step 0.2 spike: a raw AArch64 image selected with a "code:" prefix.
    // Existing callers keep the DriverIS behavior unchanged.
    constexpr std::string_view code_prefix{"code:"};
    if (filename.rfind(code_prefix, 0) == 0) {
        return std::make_unique<DriverCode>(filename.substr(code_prefix.size()));
    }

    return std::make_unique<DriverIS>(filename,
                                      skip_instructions,
                                      speculative_memory_image,
                                      use_wrong_path_emulation,
                                      validate_emulation,
                                      mem_image_path);
}

std::unique_ptr<Driver> makeDriver(std::string filename, uint64_t skip_instructions)
{
    return makeDriver(filename,
                      skip_instructions,
                      false /* speculative_memory_image */,
                      false /* use_wrong_path_emulation */,
                      0 /* validate_emulation */,
                      "" /* mem_image_path */);
}

std::unique_ptr<Driver> makeBSDriver(std::string filename)
{
    return std::make_unique<DriverBS>(filename);
}

} // namespace ampere::archx

/*
 * vim: sw=4:ts=4:et:si:cindent:cino=\:0,(0,g0,t0:
 */
