#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Track and terminate the QEMU/amperesys child processes."""

import os
import signal
import threading
import time

_lock = threading.Lock()
_children: set[int] = set()


def register_child(pid: int) -> None:
    with _lock:
        _children.add(pid)


def unregister_child(pid: int) -> None:
    with _lock:
        _children.discard(pid)


def terminate_all(grace_s: float = 2.0) -> None:
    with _lock:
        pids = list(_children)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        with _lock:
            if not _children:
                return
        time.sleep(0.05)
    with _lock:
        pids = list(_children)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
