"""Streaming/playback configuration and shared value validators."""

from __future__ import annotations

import dataclasses
import math
import os

_BLOCK_SIZE_ENV = "VISER4D_BLOCK_SIZE"
_CLIENT_CACHE_ENV = "VISER4D_CLIENT_CHUNK_CACHE_SIZE"
_DEFAULT_BLOCK_SIZE = 32
_DEFAULT_CLIENT_CACHE_BYTES = 1_000_000_000


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
            client_cache_bytes=_env_positive_int(
                _CLIENT_CACHE_ENV, _DEFAULT_CLIENT_CACHE_BYTES
            ),
        )


@dataclasses.dataclass
class PlaybackConfig:
    """Server-wide playback settings.

    Mutated in place by the server; client sessions hold a reference and read
    the current values whenever they build an outbound message.
    """

    fps: float
    streaming: StreamingConfig
    loop: bool
    speed: float


def require_positive_float(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} must be a positive finite float, got {value!r}.")
    return number


def require_positive_int(name: str, value: int) -> int:
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got {value!r}.")
    return value


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}.") from None
    return require_positive_int(name, value)
