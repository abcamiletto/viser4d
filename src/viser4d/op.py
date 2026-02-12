"""Operation recording with optional lazy disk-backed storage.

This module provides the Op class for recording scene operations. When a lazy
threshold is set, heavy payloads are offloaded to disk and loaded on demand via
an LRU cache. Disk storage uses zstd compression by default for reduced I/O.

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
    │  - In-memory data      │                                 │  - Disk-backed data    │
    │  - Stored in memory   │                                 │  - Stored on disk     │
    └───────────────────────┘                                 │  - zstd compressed    │
                                                              └───────────────────────┘
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
                                                              │  - .pkl.zst files     │
                                                              └───────────────────────┘

Compression Modes
-----------------
- NONE: No compression (.pkl files)
- FAST: zstd level 1 - fast with good compression (default)
- BALANCED: zstd level 3 - slower but better compression
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
import zstandard as zstd

# =============================================================================
# Configuration
# =============================================================================


class CompressionMode(Enum):
    """Compression mode for lazy payloads."""

    NONE = "none"
    FAST = "fast"  # zstd level 1
    BALANCED = "balanced"  # zstd level 3


_MAX_CACHE_BYTES = 1024 * 1024 * 1024  # 1GB
_DEFAULT_COMPRESSION = CompressionMode.FAST
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


def _get_zstd_level(mode: CompressionMode) -> int | None:
    """Return zstd compression level for mode, or None for no compression."""
    if mode is CompressionMode.NONE:
        return None
    if mode is CompressionMode.FAST:
        return 1
    return 3  # BALANCED


@dataclass
class _LazyPayload:
    """Disk-backed (args, kwargs) that loads on demand via LRU cache."""

    _path: Path
    _compression: CompressionMode

    @classmethod
    def save(
        cls,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        compression: CompressionMode = _DEFAULT_COMPRESSION,
    ) -> _LazyPayload:
        level = _get_zstd_level(compression)
        ext = ".pkl" if level is None else ".pkl.zst"
        path = _get_cache_dir() / f"{uuid.uuid4().hex}{ext}"

        data = cloudpickle.dumps((args, kwargs))
        if level is not None:
            cctx = zstd.ZstdCompressor(level=level)
            data = cctx.compress(data)

        with open(path, "wb") as f:
            f.write(data)

        return cls(path, compression)

    def get(self) -> tuple[tuple[Any, ...], dict[str, Any]]:
        cached = _payload_cache.get(self._path)
        if cached is not None:
            return cached

        with open(self._path, "rb") as f:
            raw = f.read()

        if self._compression is not CompressionMode.NONE:
            dctx = zstd.ZstdDecompressor()
            raw = dctx.decompress(raw)

        data = cloudpickle.loads(raw)
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
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    threshold_bytes: int | None = None,
    compression: CompressionMode = _DEFAULT_COMPRESSION,
) -> _EagerPayload | _LazyPayload:
    """Create lazy payload if data is heavy, else eager."""
    if threshold_bytes is None:
        return _EagerPayload(args, kwargs)
    if objsize.get_deep_size(args) + objsize.get_deep_size(kwargs) > threshold_bytes:
        return _LazyPayload.save(args, kwargs, compression)
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
        threshold_bytes: int | None = None,
        compression: CompressionMode = _DEFAULT_COMPRESSION,
    ) -> Op:
        """Factory that auto-selects eager vs lazy based on payload size."""
        return cls(
            kind,
            target,
            member,
            _create_payload(args, kwargs or {}, threshold_bytes, compression),
        )

    def is_lazy(self) -> bool:
        """Return True if this Op's payload is stored on disk."""
        return isinstance(self._payload, _LazyPayload)

    @property
    def args(self) -> tuple[Any, ...]:
        return self._payload.get()[0]

    @property
    def kwargs(self) -> dict[str, Any]:
        return self._payload.get()[1]
