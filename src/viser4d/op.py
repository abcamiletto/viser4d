"""Operation recording with optional lazy disk-backed storage.

This module provides the Op class for recording scene operations. Heavy payloads
(>1MB) are automatically offloaded to disk and loaded on demand via an LRU cache.

Architecture
------------
::

    ┌─────────────────────────────────────────────────────────────────────────────────────┐
    │                                        Op                                           │
    │                                                                                     │
    │   - Immutable record of a scene operation (ADD, REMOVE, SET)                        │
    │   - .args and .kwargs properties transparently load data                            │
    └─────────────────────────────────────────────────────────────────────────────────────┘
                                             │
                                             │ owns
                                             ▼
              ┌──────────────────────────────┴──────────────────────────────┐
              │                                                             │
              ▼                                                             ▼
    ┌───────────────────────┐                                 ┌───────────────────────┐
    │    _EagerPayload      │                                 │    _LazyPayload       │
    │                       │                                 │                       │
    │  - Small data (<1MB)  │                                 │  - Large data (>1MB)  │
    │  - Stored in memory   │                                 │  - Stored on disk     │
    └───────────────────────┘                                 └───────────────────────┘
                                                                          │
                                                                          │ loads via
                                                                          ▼
                                                              ┌───────────────────────┐
                                                              │    _PayloadCache      │
                                                              │                       │
                                                              │  - LRU eviction       │
                                                              │  - 1GB memory budget  │
                                                              └───────────────────────┘
                                                                          │
                                                                          │ backed by
                                                                          ▼
                                                              ┌───────────────────────┐
                                                              │   Temp directory      │
                                                              │                       │
                                                              │  - Cleaned on exit    │
                                                              │  - cloudpickle files  │
                                                              └───────────────────────┘
"""

from __future__ import annotations

import atexit
import shutil
import tempfile
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import cloudpickle
import objsize

# =============================================================================
# Configuration
# =============================================================================

_THRESHOLD_BYTES = 1024 * 1024  # 1MB
_MAX_CACHE_BYTES = 1024 * 1024 * 1024  # 1GB
_CACHE_DIR: Path | None = None


def _get_cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        _CACHE_DIR = Path(tempfile.mkdtemp(prefix="viser4d_"))
        atexit.register(_cleanup_cache_dir)
    return _CACHE_DIR


def _cleanup_cache_dir() -> None:
    global _CACHE_DIR
    if _CACHE_DIR is not None and _CACHE_DIR.exists():
        shutil.rmtree(_CACHE_DIR)
        _CACHE_DIR = None


# =============================================================================
# LRU cache for loaded payloads
# =============================================================================


class _PayloadCache:
    """LRU cache for lazy payloads with memory budget."""

    def __init__(self, max_bytes: int = _MAX_CACHE_BYTES) -> None:
        self._max_bytes = max_bytes
        self._current_bytes = 0
        self._cache: OrderedDict[Path, tuple[tuple[Any, ...], dict[str, Any]]] = (
            OrderedDict()
        )

    def get(self, path: Path) -> tuple[tuple[Any, ...], dict[str, Any]] | None:
        if path in self._cache:
            self._cache.move_to_end(path)
            return self._cache[path]
        return None

    def put(self, path: Path, data: tuple[tuple[Any, ...], dict[str, Any]]) -> None:
        size = objsize.get_deep_size(data)

        # Evict LRU entries until we have space
        while self._current_bytes + size > self._max_bytes and self._cache:
            _, evicted = self._cache.popitem(last=False)
            self._current_bytes -= objsize.get_deep_size(evicted)

        self._cache[path] = data
        self._current_bytes += size


_payload_cache = _PayloadCache()


# =============================================================================
# Payload classes
# =============================================================================


@dataclass
class _LazyPayload:
    """Disk-backed (args, kwargs) that loads on demand via LRU cache."""

    _path: Path

    @classmethod
    def save(cls, args: tuple[Any, ...], kwargs: dict[str, Any]) -> _LazyPayload:
        path = _get_cache_dir() / f"{uuid.uuid4().hex}.pkl"
        with open(path, "wb") as f:
            cloudpickle.dump((args, kwargs), f)
        return cls(path)

    def get(self) -> tuple[tuple[Any, ...], dict[str, Any]]:
        cached = _payload_cache.get(self._path)
        if cached is not None:
            return cached

        with open(self._path, "rb") as f:
            data = cloudpickle.load(f)

        _payload_cache.put(self._path, data)
        return data


@dataclass(frozen=True)
class _EagerPayload:
    """In-memory (args, kwargs)."""

    _args: tuple[Any, ...]
    _kwargs: dict[str, Any]

    def get(self) -> tuple[tuple[Any, ...], dict[str, Any]]:
        return (self._args, self._kwargs)


def _create_payload(
    args: tuple[Any, ...], kwargs: dict[str, Any]
) -> _EagerPayload | _LazyPayload:
    """Create lazy payload if data is heavy, else eager."""
    if objsize.get_deep_size(args) + objsize.get_deep_size(kwargs) > _THRESHOLD_BYTES:
        return _LazyPayload.save(args, kwargs)
    return _EagerPayload(args, kwargs)


# =============================================================================
# Op
# =============================================================================


class OpKind(Enum):
    ADD = "add"
    REMOVE = "remove"
    SET = "set"


@dataclass(frozen=True)
class Op:
    """Recorded scene operation with transparent lazy loading for heavy data."""

    kind: OpKind
    target: str
    member: str
    _payload: _EagerPayload | _LazyPayload = field(repr=False)

    @classmethod
    def create(
        cls,
        kind: OpKind,
        target: str,
        member: str,
        args: tuple[Any, ...] = (),
        kwargs: dict[str, Any] | None = None,
    ) -> Op:
        """Factory that auto-selects eager vs lazy based on payload size."""
        return cls(kind, target, member, _create_payload(args, kwargs or {}))

    @property
    def args(self) -> tuple[Any, ...]:
        return self._payload.get()[0]

    @property
    def kwargs(self) -> dict[str, Any]:
        return self._payload.get()[1]
