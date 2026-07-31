"""Streaming configuration and shared value validators."""

from __future__ import annotations

import dataclasses
import math
import os
import re

_BLOCK_SIZE_ENV = "VISER4D_BLOCK_SIZE"
_CLIENT_CACHE_ENV = "VISER4D_CLIENT_CHUNK_CACHE_SIZE"
_DEFAULT_BLOCK_SIZE = 32
_DEFAULT_CLIENT_CACHE_BYTES = 1_000_000_000

_BYTE_UNITS = {
    "": 1,
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "KIB": 1 << 10,
    "MIB": 1 << 20,
    "GIB": 1 << 30,
    "TIB": 1 << 40,
}
_BYTE_SIZE_PATTERN = re.compile(r"^\s*(\d+)\s*([A-Za-z]*)\s*$")


def require_positive_float(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} must be a positive finite float, got {value!r}.")
    return number


def require_positive_int(name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}.")
    return value


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = 0
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}.")
    return value


def _env_byte_size(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    match = _BYTE_SIZE_PATTERN.fullmatch(raw)
    unit = match.group(2).upper() if match else None
    multiplier = _BYTE_UNITS.get(unit) if unit is not None else None
    if match is None or multiplier is None:
        raise ValueError(
            f"{name} must be an integer byte count or a size like '512MB' or '1GiB', "
            f"got {raw!r}."
        )
    value = int(match.group(1)) * multiplier
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}.")
    return value


@dataclasses.dataclass(frozen=True)
class StreamingConfig:
    """Server-owned block sizing and per-client preload budget."""

    block_size: int = _DEFAULT_BLOCK_SIZE
    client_cache_bytes: int = _DEFAULT_CLIENT_CACHE_BYTES

    def __post_init__(self) -> None:
        require_positive_int("block_size", self.block_size)
        require_positive_int("client_cache_bytes", self.client_cache_bytes)

    @classmethod
    def from_env(cls) -> StreamingConfig:
        return cls(
            block_size=_env_positive_int(_BLOCK_SIZE_ENV, _DEFAULT_BLOCK_SIZE),
            client_cache_bytes=_env_byte_size(
                _CLIENT_CACHE_ENV, _DEFAULT_CLIENT_CACHE_BYTES
            ),
        )
