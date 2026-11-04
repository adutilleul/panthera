//
// Copyright (c) 2021-2022 Ampere Computing. All rights reserved.
// SPDX-License-Identifier: BSD-3-Clause
//

#include <google/protobuf/io/zero_copy_stream_impl.h>
#include <boost/filesystem.hpp>

#include "protobuf_utils.h"

#pragma once

namespace ampere {

class ProtobufStreamWriter
{
public:
    // fd constructors take ownership of the descriptor.
    ProtobufStreamWriter(int fd, uint32_t magic_number, uint32_t version);
    ProtobufStreamWriter(boost::filesystem::path filepath, uint32_t magic_number, uint32_t version, bool force = false);
    ~ProtobufStreamWriter();
    template<typename Item>
    bool write(const Item& item);
    bool flush();

protected:
    boost::filesystem::path filepath;
    std::unique_ptr<google::protobuf::io::FileOutputStream> output_stream;

private:
    FILE* fp{nullptr};
    bool fp_is_pipe{false};
};

template<typename Item>
bool ProtobufStreamWriter::write(const Item& item)
{
    return writeDelimitedTo(item, output_stream.get());
}

class ProtobufStreamReader
{
public:
    // fd constructors take ownership of the descriptor.
    ProtobufStreamReader(int fd, uint32_t magic_number, uint32_t max_version);
    ProtobufStreamReader(boost::filesystem::path filepath, uint32_t magic_number, uint32_t max_version);
    ~ProtobufStreamReader();
    uint32_t version() const { return stream_version; }
    template<typename Item>
    bool read(Item& item);

protected:
    boost::filesystem::path filepath;
    std::unique_ptr<google::protobuf::io::FileInputStream> input_stream;

private:
    FILE* fp{nullptr};
    bool fp_is_pipe{false};
    void verify_file_version(uint32_t magic_number, uint32_t max_version);
    uint32_t stream_version;
};

template<typename Item>
bool ProtobufStreamReader::read(Item& item)
{
    return readDelimitedFrom(input_stream.get(), &item, nullptr);
}

} // namespace ampere

/*
 * vim: sw=4:ts=4:et:si:cindent:cino=\:0,(0,g0,t0:
 */
