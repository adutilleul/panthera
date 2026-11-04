#!/usr/bin/env python3
# Copyright (c) 2026 Ampere Computing. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
"""Content-addressed trace and result caches for the microbenchmark sidecar."""

import hashlib
import json
import os
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from qemu_trace_source import TraceResult
from simulation_runner import SimulationResult

CACHE_SCHEMA = 1


def file_fingerprint(path: str | os.PathLike | None, hash_contents: bool = False) -> dict:
    if path is None:
        return {"missing": True}
    entry = Path(path)
    if not entry.exists():
        return {"path": str(entry), "missing": True}
    stat = entry.stat()
    fingerprint = {
        "path": str(entry.resolve()),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }
    if hash_contents and entry.is_file():
        fingerprint["sha256"] = hashlib.sha256(entry.read_bytes()).hexdigest()
    return fingerprint


def content_key(payload: dict) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


CFG_INCLUDE = re.compile(
    r"^\s*(?:(?P<scope>[^\s:#]+):)?"
    r"(?P<directive>cfg|append-cfg|override-cfg|:cfg|:append-cfg|:override-cfg)"
    r"\s+(?P<file>[^\s#]+)"
)


def config_fingerprint(path: str | os.PathLike | None, seen: set[str] | None = None, depth: int = 0) -> dict:
    """Hash a config and every file reachable through :cfg/:append-cfg/:override-cfg."""
    if path is None:
        return {"missing": True}
    entry = Path(path)
    if not entry.exists():
        return {"path": str(entry), "missing": True}
    if depth > 32:
        return {"path": str(entry.resolve()), "error": "include depth"}
    resolved = str(entry.resolve())
    if seen is None:
        seen = set()
    if resolved in seen:
        return {"path": resolved, "cycle": True}
    seen.add(resolved)
    digest = hashlib.sha256(entry.read_bytes()).hexdigest()
    includes = []
    try:
        lines = entry.read_text(errors="replace").splitlines()
    except OSError:
        lines = []
    for line in lines:
        match = CFG_INCLUDE.match(line)
        if not match:
            continue
        child = Path(match.group("file").strip("'\""))
        if not child.is_absolute():
            child = entry.parent / child
        includes.append(config_fingerprint(child, seen, depth + 1))
    return {"path": resolved, "sha256": digest, "includes": includes}


def _entry_size(entry: Path) -> int:
    if entry.is_file():
        return entry.stat().st_size
    return sum(item.stat().st_size for item in entry.rglob("*") if item.is_file())


def _evict(root: Path, max_bytes: int | None, new_size: int = 0) -> None:
    if max_bytes is None:
        return
    entries = []
    for entry in root.iterdir():
        if entry.name.startswith("."):
            continue
        try:
            entries.append((entry, entry.stat().st_mtime, _entry_size(entry)))
        except OSError:
            continue
    total = sum(size for _, _, size in entries)
    for entry, _, size in sorted(entries, key=lambda item: item[1]):
        if total + new_size <= max_bytes:
            break
        total -= size
        try:
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            pass


class ResultCache:
    """Cache final simulation metrics keyed by request + tool fingerprints."""

    def __init__(self, root: str | os.PathLike, max_bytes: int | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def key(self, payload: dict) -> str:
        return content_key({"schema": CACHE_SCHEMA, **payload})

    def get(self, key: str) -> SimulationResult | None:
        path = self.root / f"{key}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            result = SimulationResult(
                cycles=int(data["cycles"]),
                instructions=int(data["instructions"]),
                ipc=float(data["ipc"]),
                wall_ns=int(data.get("wall_ns", 0)),
                log=str(data.get("summary", "")),
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None
        try:
            os.utime(path, None)
        except OSError:
            return None
        return result

    def put(self, key: str, result: SimulationResult) -> None:
        path = self.root / f"{key}.json"
        data = {
            "schema": CACHE_SCHEMA,
            "cycles": result.cycles,
            "instructions": result.instructions,
            "ipc": result.ipc,
            "wall_ns": result.wall_ns,
            "summary": result.summary(),
            "created_ns": time.time_ns(),
        }
        payload = json.dumps(data, sort_keys=True)
        with self._lock:
            _evict(self.root, self.max_bytes, len(payload))
            tmp = self.root / f".tmp-{key}-{uuid.uuid4().hex}"
            tmp.write_text(payload)
            os.replace(tmp, path)


class TraceCache:
    """Cache generated `.is.xz`/`.mem` artifacts keyed by code + tools."""

    def __init__(self, root: str | os.PathLike, max_bytes: int | None = None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    def key(self, payload: dict) -> str:
        return content_key({"schema": CACHE_SCHEMA, **payload})

    def get(self, key: str) -> TraceResult | None:
        entry = self.root / key
        meta_path = entry / "meta.json"
        trace_path = entry / "trace.is.xz"
        mem_path = entry / "trace.mem"
        if not entry.exists():
            return None
        if not (meta_path.exists() and trace_path.exists() and mem_path.exists()):
            shutil.rmtree(entry, ignore_errors=True)
            return None
        try:
            meta = json.loads(meta_path.read_text())
            if trace_path.stat().st_size == 0 or mem_path.stat().st_size == 0:
                raise ValueError("empty trace artifact")
            if "trace_size" in meta and trace_path.stat().st_size != int(meta["trace_size"]):
                raise ValueError("trace size mismatch")
            if "mem_size" in meta and mem_path.stat().st_size != int(meta["mem_size"]):
                raise ValueError("mem size mismatch")
            result = TraceResult(
                path=trace_path,
                instructions=int(meta["instructions"]),
                warmup=int(meta["warmup"]),
                measurement=int(meta["measurement"]),
                cooldown=int(meta["cooldown"]),
                memops=int(meta["memops"]),
                memory_events=int(meta["memory_events"]),
                sysreg_events=int(meta["sysreg_events"]),
                generation_ns=0,
                code_sha256=str(meta["code_sha256"]),
                mem_path=mem_path,
                log="",
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            shutil.rmtree(entry, ignore_errors=True)
            return None
        try:
            os.utime(entry, None)
        except OSError:
            return None
        return result

    def put(self, key: str, result: TraceResult) -> TraceResult:
        final = self.root / key
        if result.mem_path is None:
            raise ValueError("trace cache requires a .mem image")
        if final.exists():
            cached = self.get(key)
            if cached is not None:
                return cached
            shutil.rmtree(final, ignore_errors=True)

        tmp = self.root / f".tmp-{key}-{uuid.uuid4().hex}"
        tmp.mkdir()
        try:
            shutil.copy2(result.path, tmp / "trace.is.xz")
            shutil.copy2(result.mem_path, tmp / "trace.mem")
            meta = {
                "schema": CACHE_SCHEMA,
                "instructions": result.instructions,
                "warmup": result.warmup,
                "measurement": result.measurement,
                "cooldown": result.cooldown,
                "memops": result.memops,
                "memory_events": result.memory_events,
                "sysreg_events": result.sysreg_events,
                "code_sha256": result.code_sha256,
                "trace_size": (tmp / "trace.is.xz").stat().st_size,
                "mem_size": (tmp / "trace.mem").stat().st_size,
                "created_ns": time.time_ns(),
            }
            (tmp / "meta.json").write_text(json.dumps(meta, sort_keys=True))
            with self._lock:
                _evict(self.root, self.max_bytes, _entry_size(tmp))
                try:
                    os.replace(tmp, final)
                except OSError:
                    # Another writer won the race; use its entry if valid.
                    shutil.rmtree(tmp, ignore_errors=True)
                    cached = self.get(key)
                    if cached is not None:
                        return cached
                    raise
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise

        return TraceResult(
            path=final / "trace.is.xz",
            instructions=result.instructions,
            warmup=result.warmup,
            measurement=result.measurement,
            cooldown=result.cooldown,
            memops=result.memops,
            memory_events=result.memory_events,
            sysreg_events=result.sysreg_events,
            generation_ns=0,
            code_sha256=result.code_sha256,
            mem_path=final / "trace.mem",
            log="",
        )
