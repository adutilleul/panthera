//
// Copyright (c) 2019 Ampere Computing. All rights reserved.
// SPDX-License-Identifier: BSD-3-Clause
//
#include "ampere/instruction_stream/instruction_stream.h"
#include <boost/filesystem/operations.hpp>
#include <cerrno>
#include <fcntl.h>
#include <gtest/gtest.h>
#include <unistd.h>

namespace ampere {
namespace is {

using namespace boost::filesystem;

class InstructionStreamReaderTest : public ::testing::Test
{
protected:
    std::string testfixture(std::string filename) { return (path(TEST_FIXTURE_DIRECTORY) / filename).string(); }
};

TEST_F(InstructionStreamReaderTest, testMissingFile)
{
    ASSERT_THROW(InstructionStreamReader is("doesnotexist.is"), std::system_error);
}

TEST_F(InstructionStreamReaderTest, testMissingXZFile)
{
    ASSERT_THROW(InstructionStreamReader is("doesnotexist.is.xz"), std::system_error);
}

TEST_F(InstructionStreamReaderTest, testRead)
{
    InstructionStreamReader is(testfixture("test.is"));
    Instruction instruction;
    ASSERT_TRUE(is.read(instruction));
    ASSERT_TRUE(is.read(instruction));
    ASSERT_FALSE(is.read(instruction));
    ASSERT_EQ(is.version(), 1);

    // are the feature bits all false, since we are reading version 1?
    ASSERT_FALSE(is.features().divide_sqrt_registers());
    ASSERT_FALSE(is.features().cache_maintenance_registers());
    ASSERT_FALSE(is.features().memory_translation());
    ASSERT_FALSE(is.features().memory_values());
}

TEST_F(InstructionStreamReaderTest, testReadXZ)
{
    InstructionStreamReader is(testfixture("test.is.xz"));
    Instruction instruction;
    ASSERT_TRUE(is.read(instruction));
    ASSERT_TRUE(is.read(instruction));
    ASSERT_FALSE(is.read(instruction));
    ASSERT_EQ(is.version(), 1);

    // are the feature bits all false, since we are reading version 1?
    ASSERT_FALSE(is.features().divide_sqrt_registers());
    ASSERT_FALSE(is.features().cache_maintenance_registers());
    ASSERT_FALSE(is.features().memory_translation());
    ASSERT_FALSE(is.features().memory_values());
}

TEST_F(InstructionStreamReaderTest, testReadFromFd)
{
    int fds[2];
    ASSERT_EQ(pipe(fds), 0);

    {
        is::Features features;
        features.set_memory_translation(true);
        is::InstructionStreamWriter writer(fds[1], features);

        is::Event start;
        start.mutable_control()->set_type(is::Control_Type_START_MEASUREMENT);
        ASSERT_TRUE(writer.write(start));

        is::Instruction instruction;
        instruction.set_opcode(0x12345678);
        ASSERT_TRUE(writer.write(instruction));
        instruction.set_opcode(0x87654321);
        ASSERT_TRUE(writer.write(instruction));

        is::Event stop;
        stop.mutable_control()->set_type(is::Control_Type_STOP_MEASUREMENT);
        ASSERT_TRUE(writer.write(stop));
        writer.finalize();
    }
    // The fd-based writer takes ownership of its descriptor.
    errno = 0;
    EXPECT_EQ(fcntl(fds[1], F_GETFD), -1);
    EXPECT_EQ(errno, EBADF);

    {
        InstructionStreamReader reader(fds[0]);
        ASSERT_TRUE(reader.features().memory_translation());

        Instruction instruction;
        ASSERT_TRUE(reader.read(instruction));
        ASSERT_EQ(instruction.opcode(), 0x12345678);
        ASSERT_TRUE(reader.read(instruction));
        ASSERT_EQ(instruction.opcode(), 0x87654321);
        ASSERT_FALSE(reader.read(instruction));
    }
    // The fd-based reader takes ownership of its descriptor.
    errno = 0;
    EXPECT_EQ(fcntl(fds[0], F_GETFD), -1);
    EXPECT_EQ(errno, EBADF);
}

} // namespace is
} // namespace ampere

/*
 * vim: sw=4:ts=4:et:si:cindent:cino=\:0,(0,g0,t0:
 */
