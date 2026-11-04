# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause

# The v1 sidecar is Python. Its files are always installed; test registration
# is gated on the optional runtime dependencies (protoc and grpcio) so the
# main build does not fail on hosts without them.
add_subdirectory(python)
