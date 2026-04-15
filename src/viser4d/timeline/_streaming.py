from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass

from .._validation import env_byte_size, env_positive_int, require_positive_int


_BLOCK_SIZE_ENV = "VISER4D_BLOCK_SIZE"
_DEFAULT_BLOCK_SIZE = 32
_CLIENT_CHUNK_CACHE_SIZE_ENV = "VISER4D_CLIENT_CHUNK_CACHE_SIZE"
_DEFAULT_CLIENT_CHUNK_CACHE_BYTES = 1_000_000_000


@dataclass(frozen=True)
class ChunkStreamingConfig:
    """Server-owned chunking and preload settings."""

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
    block_index: int
    step_start: int
    step_stop: int
    checkpoint_block_index: int | None
    payload_byte_size: int | None
    dirty: bool
    revision: int


@dataclass(frozen=True)
class PreloadPlan:
    desired_blocks: tuple[int, ...]
    required_loads: tuple[int, ...]
    speculative_loads: tuple[int, ...]
    evictions: tuple[int, ...]


class PreloadPlanner:
    @staticmethod
    def plan(
        current_block: int,
        manifests: Sequence[BlockManifest],
        budget_bytes: int,
        *,
        loaded_blocks: Collection[int] = (),
        pending_blocks: Collection[int] = (),
        force: bool = False,
    ) -> PreloadPlan:
        block_count = len(manifests)
        if block_count == 0:
            return PreloadPlan((), (), (), ())

        required_blocks = [current_block]
        used_bytes = _known_block_bytes(manifests[current_block])

        previous_block = (current_block - 1) % block_count
        if previous_block != current_block:
            required_blocks.append(previous_block)
            used_bytes += _known_block_bytes(manifests[previous_block])

        desired_blocks = list(required_blocks)
        desired_set = set(desired_blocks)
        speculative_blocks: list[int] = []
        for offset in range(1, block_count):
            if used_bytes >= budget_bytes:
                break
            block_index = (current_block + offset) % block_count
            if block_index in desired_set:
                continue
            block_bytes = manifests[block_index].payload_byte_size
            if block_bytes is None:
                speculative_blocks.append(block_index)
                break
            if used_bytes + block_bytes > budget_bytes:
                break
            speculative_blocks.append(block_index)
            used_bytes += block_bytes
        desired_blocks.extend(speculative_blocks)

        resident_blocks = set(loaded_blocks)
        resident_blocks.update(pending_blocks)
        if force:
            required_loads = tuple(required_blocks)
            speculative_loads = tuple(speculative_blocks)
        else:
            required_loads = tuple(
                block_index
                for block_index in required_blocks
                if block_index not in resident_blocks
            )
            speculative_loads = tuple(
                block_index
                for block_index in speculative_blocks
                if block_index not in resident_blocks
            )

        desired_set = set(desired_blocks)
        evictions = tuple(sorted(set(loaded_blocks) - desired_set))
        return PreloadPlan(
            desired_blocks=tuple(desired_blocks),
            required_loads=required_loads,
            speculative_loads=speculative_loads,
            evictions=evictions,
        )


def _known_block_bytes(manifest: BlockManifest) -> int:
    return 0 if manifest.payload_byte_size is None else manifest.payload_byte_size
