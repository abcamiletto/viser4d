from __future__ import annotations

from dataclasses import dataclass

from .._validation import env_byte_size, env_positive_int, require_positive_int


_BLOCK_SIZE_ENV = "VISER4D_BLOCK_SIZE"
_DEFAULT_BLOCK_SIZE = 32
_CLIENT_CHUNK_CACHE_SIZE_ENV = "VISER4D_CLIENT_CHUNK_CACHE_SIZE"
_DEFAULT_CLIENT_CHUNK_CACHE_BYTES = 1_000_000_000


@dataclass(frozen=True)
class ChunkStreamingConfig:
    """Server-owned chunking and client preload settings."""

    block_size: int = _DEFAULT_BLOCK_SIZE
    client_chunk_cache_bytes: int = _DEFAULT_CLIENT_CHUNK_CACHE_BYTES

    def __post_init__(self) -> None:
        require_positive_int("block_size", self.block_size)
        require_positive_int(
            "client_chunk_cache_bytes",
            self.client_chunk_cache_bytes,
        )

    @classmethod
    def from_env(cls) -> ChunkStreamingConfig:
        return cls(
            block_size=env_positive_int(_BLOCK_SIZE_ENV, _DEFAULT_BLOCK_SIZE),
            client_chunk_cache_bytes=env_byte_size(
                _CLIENT_CHUNK_CACHE_SIZE_ENV,
                _DEFAULT_CLIENT_CHUNK_CACHE_BYTES,
            ),
        )


@dataclass(frozen=True)
class BlockManifest:
    """Lightweight per-block metadata used by the client's preload planner."""

    block_index: int
    step_start: int
    step_stop: int
    payload_byte_size: int | None
    dirty: bool
