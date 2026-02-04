"""Operation recording with optional lazy disk-backed storage."""

from __future__ import annotations

import tempfile
import uuid
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
_CACHE_DIR: Path | None = None


def _get_cache_dir() -> Path:
    global _CACHE_DIR
    if _CACHE_DIR is None:
        _CACHE_DIR = Path(tempfile.mkdtemp(prefix="viser4d_"))
    return _CACHE_DIR


@dataclass
class _LazyPayload:
    """Disk-backed (args, kwargs) that loads on demand."""

    _path: Path
    _cached: tuple[tuple[Any, ...], dict[str, Any]] | None = field(
        default=None, repr=False
    )

    @classmethod
    def save(cls, args: tuple[Any, ...], kwargs: dict[str, Any]) -> _LazyPayload:
        path = _get_cache_dir() / f"{uuid.uuid4().hex}.pkl"
        with open(path, "wb") as f:
            cloudpickle.dump((args, kwargs), f)
        return cls(path)

    def get(self) -> tuple[tuple[Any, ...], dict[str, Any]]:
        if self._cached is None:
            with open(self._path, "rb") as f:
                self._cached = cloudpickle.load(f)
        return self._cached


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
