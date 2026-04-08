"""Block-backed timeline storage with LRU caching and checkpoint management."""

from __future__ import annotations

import math
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from pathlib import Path

import msgspec
import zstandard

from .. import _viser_private as impl
from ..audio._messages import is_audio_message
from .._types import RuntimeBlockPayload, StoredMessage
from ._checkpoint import (
    CheckpointState,
    apply_steps,
    checkpoint_messages,
    copy_checkpoint,
    load_checkpoint_file,
    write_checkpoint_file,
)
from ._messages_util import (
    TimelineStep,
    extract_message_name,
    is_scene_message,
    store_raw_message,
)


@dataclass
class TimelineBlock:
    """In-memory representation of one block of recorded timesteps."""

    steps: list[TimelineStep]
    dirty: bool = False


class _BlockFilePayload(msgspec.Struct):
    sceneSteps: list[list[tuple[str, StoredMessage]]]
    audioSteps: list[list[StoredMessage]]


def _is_same_node_or_descendant(name: str, root: str) -> bool:
    return name == root or name.startswith(f"{root}/")


class TimelineStore:
    """Block-backed storage for timeline-owned steps and global scene overrides."""

    def __init__(
        self,
        num_steps: int,
        *,
        block_size: int = 64,
        max_loaded_blocks: int = 4,
        max_cached_checkpoints: int = 4,
        flush_executor: Executor,
    ) -> None:
        if block_size < 1:
            raise ValueError(f"block_size must be >= 1, got {block_size}.")
        if max_loaded_blocks < 1:
            raise ValueError(
                f"max_loaded_blocks must be >= 1, got {max_loaded_blocks}."
            )
        if max_cached_checkpoints < 1:
            raise ValueError(
                f"max_cached_checkpoints must be >= 1, got {max_cached_checkpoints}."
            )
        self.num_steps = num_steps
        self.block_size = block_size
        self._node_start_steps: dict[str, int] = {}
        self._global_overrides: dict[str, StoredMessage] = {}
        self._lock = threading.RLock()
        self._loaded_blocks: OrderedDict[int, TimelineBlock] = OrderedDict()
        self._max_loaded_blocks = max_loaded_blocks
        self._checkpoint_cache: OrderedDict[int, CheckpointState] = OrderedDict()
        self._max_cached_checkpoints = max_cached_checkpoints
        self._flush_executor = flush_executor
        self._pending_flushes: dict[int, Future[None]] = {}
        self._block_dir_root = tempfile.TemporaryDirectory(prefix="viser4d-timeline-")
        self._block_dir = Path(self._block_dir_root.name)
        # Running checkpoint for eager incremental persistence during sequential recording.
        self._eager_checkpoint = CheckpointState()
        self._eager_checkpoint_next_block = 0

    def validate_step(self, step: int) -> int:
        """Return ``step`` if it is in range, else raise ``IndexError``."""
        if step < 0 or step >= self.num_steps:
            raise IndexError(
                f"Timestep {step} is out of range for {self.num_steps} steps."
            )
        return step

    @property
    def block_count(self) -> int:
        return math.ceil(self.num_steps / self.block_size)

    def step(self, step: int) -> TimelineStep:
        """Return the storage bucket for one timestep."""
        with self._lock:
            step = self.validate_step(step)
            block = self._load_block(step // self.block_size)
            return block.steps[step % self.block_size]

    def has_node(self, name: str) -> bool:
        with self._lock:
            return name in self._node_start_steps

    def record_step(self, step: int, messages: list[impl.Message]) -> None:
        """Store one timestep's scene and audio updates."""
        with self._lock:
            step = self.validate_step(step)
            block_index = step // self.block_size
            block = self._load_block(block_index)
            step_state = block.steps[step % self.block_size]
            for message in messages:
                stored_message = store_raw_message(message)
                key = message.redundancy_key()
                name = extract_message_name(stored_message)
                if is_scene_message(stored_message):
                    if name is not None and impl.is_create_scene_node_message(message):
                        self._node_start_steps[name] = step
                    step_state.scene_updates.pop(key, None)
                    step_state.scene_updates[key] = stored_message
                    continue
                if is_audio_message(message):
                    step_state.audio_updates.append(stored_message)
            block.dirty = True
            self._invalidate_checkpoints_after_block(block_index)

    def record_global_override(self, message: impl.Message) -> None:
        """Store a live scene override that should replay for all future steps."""
        with self._lock:
            stored_message = store_raw_message(message)
            node_name = extract_message_name(stored_message)
            redundancy_key = message.redundancy_key()
            if stored_message.payload.get(
                "type"
            ) == "RemoveSceneNodeMessage" and isinstance(node_name, str):
                self._prune_removed_node_overrides(node_name)
            self._global_overrides.pop(redundancy_key, None)
            self._global_overrides[redundancy_key] = stored_message

    def empty_copy(self, num_steps: int | None = None) -> TimelineStore:
        """Return a fresh timeline store with matching storage settings."""
        return TimelineStore(
            self.num_steps if num_steps is None else num_steps,
            block_size=self.block_size,
            max_loaded_blocks=self._max_loaded_blocks,
            max_cached_checkpoints=self._max_cached_checkpoints,
            flush_executor=self._flush_executor,
        )

    def resized_copy(self, num_steps: int) -> TimelineStore:
        """Clone this timeline into a new store with ``num_steps`` timesteps."""
        with self._lock:
            resized = self.empty_copy(num_steps)
            copied_steps = min(self.num_steps, num_steps)
            copied_blocks = math.ceil(copied_steps / self.block_size)
            for block_index in range(copied_blocks):
                source_block = self._load_block(block_index)
                target_block = resized._load_block(block_index)
                target_block.dirty = True
                block_start = block_index * self.block_size
                copied_step_count = min(
                    len(target_block.steps),
                    copied_steps - block_start,
                )
                for offset in range(copied_step_count):
                    source_step = source_block.steps[offset]
                    target_step = target_block.steps[offset]
                    target_step.scene_updates = dict(source_step.scene_updates)
                    target_step.audio_updates = list(source_step.audio_updates)

            resized._node_start_steps = {
                name: step
                for name, step in self._node_start_steps.items()
                if step < copied_steps
            }
            resized._global_overrides = self._copy_live_global_overrides(
                resized._node_start_steps
            )

            return resized

    def _prune_removed_node_overrides(self, removed_node: str) -> None:
        remaining_overrides: dict[str, StoredMessage] = {}
        for key, message in self._global_overrides.items():
            node_name = extract_message_name(message)
            is_removed_subtree = node_name is not None and _is_same_node_or_descendant(
                node_name, removed_node
            )
            if is_removed_subtree:
                continue
            remaining_overrides[key] = message
        self._global_overrides = remaining_overrides

    def _copy_live_global_overrides(
        self, live_nodes: dict[str, int]
    ) -> dict[str, StoredMessage]:
        overrides: dict[str, StoredMessage] = {}
        for key, message in self._global_overrides.items():
            node_name = extract_message_name(message)
            is_stale_override = node_name is not None and node_name not in live_nodes
            if is_stale_override:
                continue
            overrides[key] = message
        return overrides

    def messages_for_step(self, step: int) -> list[StoredMessage]:
        """Return all messages visible at ``step``, including global overrides."""
        with self._lock:
            step = self.validate_step(step)
            block = self._load_block(step // self.block_size)
            return self._merged_step_messages(step, block.steps[step % self.block_size])

    def block_index_for_step(self, step: int) -> int:
        return self.validate_step(step) // self.block_size

    def block_payload(self, block_index: int) -> RuntimeBlockPayload:
        """Build the checkpoint-plus-step payload consumed by the browser runtime."""
        with self._lock:
            block_index = self._validate_block_index(block_index)
            ckpt = self._checkpoint_for_block(block_index)
            block = self._load_block(block_index)
            block_start = block_index * self.block_size
            step_messages = [
                self._merged_step_messages(block_start + offset, step_state)
                for offset, step_state in enumerate(block.steps)
            ]
        ckpt_messages = checkpoint_messages(ckpt)
        return {
            "block": block_index,
            "checkpointMessages": ckpt_messages,
            "stepMessages": step_messages,
        }

    def _merged_step_messages(
        self,
        step: int,
        step_state: TimelineStep,
    ) -> list[StoredMessage]:
        return [
            *step_state.scene_updates.values(),
            *step_state.audio_updates,
            *self._global_override_messages_for_step(step),
        ]

    def _global_override_messages_for_step(self, step: int) -> list[StoredMessage]:
        messages: list[StoredMessage] = []
        for message in self._global_overrides.values():
            node_name = extract_message_name(message)
            node_started_later = (
                isinstance(node_name, str)
                and self._node_start_steps.get(node_name, 0) > step
            )
            if node_started_later:
                continue
            messages.append(message)
        return messages

    def close(self) -> None:
        """Flush any dirty blocks and release temporary on-disk storage."""
        with self._lock:
            try:
                for block_index, block in list(self._loaded_blocks.items()):
                    self._flush_block(block_index, block)
                self._loaded_blocks.clear()
                for block_index in tuple(self._pending_flushes):
                    self._wait_for_pending_flush(block_index)
            finally:
                self._block_dir_root.cleanup()

    def _block_path(self, block_index: int) -> Path:
        return self._block_dir / f"{block_index:08d}.msgpack.zst"

    def _checkpoint_path(self, block_index: int) -> Path:
        return self._block_dir / f"checkpoint_{block_index:08d}.msgpack.zst"

    def _validate_block_index(self, block_index: int) -> int:
        if block_index < 0 or block_index >= self.block_count:
            raise IndexError(
                f"Block {block_index} is out of range for {self.block_count} blocks."
            )
        return block_index

    def _load_block(self, block_index: int) -> TimelineBlock:
        block_index = self._validate_block_index(block_index)
        block = self._loaded_blocks.get(block_index)
        if block is not None:
            self._loaded_blocks.move_to_end(block_index)
            return block
        self._wait_for_pending_flush(block_index)
        path = self._block_path(block_index)
        if path.exists():
            raw = zstandard.ZstdDecompressor().decompress(path.read_bytes())
            payload = msgspec.msgpack.decode(raw, type=_BlockFilePayload)
            block = TimelineBlock(
                steps=[
                    TimelineStep(
                        scene_updates=dict(scene_updates),
                        audio_updates=audio_updates,
                    )
                    for scene_updates, audio_updates in zip(
                        payload.sceneSteps,
                        payload.audioSteps,
                        strict=True,
                    )
                ],
                dirty=False,
            )
        else:
            step_count = min(
                self.block_size, self.num_steps - block_index * self.block_size
            )
            block = TimelineBlock(steps=[TimelineStep() for _ in range(step_count)])
        self._loaded_blocks[block_index] = block
        self._evict_loaded_blocks()
        return block

    def _evict_loaded_blocks(self) -> None:
        while len(self._loaded_blocks) > self._max_loaded_blocks:
            block_index, block = self._loaded_blocks.popitem(last=False)
            self._flush_block(block_index, block)

    def _flush_block(self, block_index: int, block: TimelineBlock) -> None:
        if not block.dirty:
            return
        block.dirty = False
        pending_flush = self._pending_flushes.get(block_index)
        # Eagerly compute the checkpoint for block_index+1 while we still have
        # the block data in hand, so seeks never need to replay prior blocks.
        # Only possible when blocks are flushed in contiguous order from 0.
        next_block = block_index + 1
        ckpt: tuple[Path, CheckpointState] | None = None
        if (
            block_index == self._eager_checkpoint_next_block
            and next_block < self.block_count
        ):
            apply_steps(self._eager_checkpoint, block.steps)
            self._eager_checkpoint_next_block = next_block
            ckpt = (
                self._checkpoint_path(next_block),
                copy_checkpoint(self._eager_checkpoint),
            )
        self._pending_flushes[block_index] = self._flush_executor.submit(
            _write_block_and_checkpoint_after,
            pending_flush,
            self._block_path(block_index),
            block,
            ckpt,
        )

    def _wait_for_pending_flush(self, block_index: int) -> None:
        future = self._pending_flushes.pop(block_index, None)
        if future is not None:
            future.result()

    def _invalidate_checkpoints_after_block(self, block_index: int) -> None:
        stale = [i for i in self._checkpoint_cache if i > block_index]
        for i in stale:
            del self._checkpoint_cache[i]

    def _checkpoint_for_block(self, block_index: int) -> CheckpointState:
        block_index = self._validate_block_index(block_index)
        cached = self._checkpoint_cache.get(block_index)
        if cached is not None:
            self._checkpoint_cache.move_to_end(block_index)
            return copy_checkpoint(cached)
        # Check disk before falling back to block replay.
        disk_state = self._load_checkpoint_from_disk(block_index)
        if disk_state is not None:
            self._cache_checkpoint(block_index, disk_state)
            return disk_state
        prior_blocks = [i for i in self._checkpoint_cache if i < block_index]
        base_index = max(prior_blocks, default=0)
        state = (
            copy_checkpoint(self._checkpoint_cache[base_index])
            if base_index in self._checkpoint_cache
            else CheckpointState()
        )
        for index in range(base_index, block_index):
            block = self._load_block(index)
            apply_steps(state, block.steps)
            self._cache_checkpoint(index + 1, state)
        return copy_checkpoint(state)

    def _load_checkpoint_from_disk(self, block_index: int) -> CheckpointState | None:
        # Wait for any in-flight flush that might be writing this checkpoint.
        if block_index > 0:
            pending = self._pending_flushes.get(block_index - 1)
            if pending is not None:
                pending.result()
        path = self._checkpoint_path(block_index)
        return load_checkpoint_file(path) if path.exists() else None

    def _cache_checkpoint(self, block_index: int, state: CheckpointState) -> None:
        self._checkpoint_cache[block_index] = copy_checkpoint(state)
        self._checkpoint_cache.move_to_end(block_index)
        while len(self._checkpoint_cache) > self._max_cached_checkpoints:
            self._checkpoint_cache.popitem(last=False)


def _write_block_file(path: Path, block: TimelineBlock) -> None:
    payload = _BlockFilePayload(
        sceneSteps=[list(step.scene_updates.items()) for step in block.steps],
        audioSteps=[step.audio_updates for step in block.steps],
    )
    packed = msgspec.msgpack.encode(payload)
    compressed = zstandard.ZstdCompressor(level=6).compress(packed)
    path.write_bytes(compressed)


def _write_block_and_checkpoint_after(
    previous_flush: Future[None] | None,
    block_path: Path,
    block: TimelineBlock,
    checkpoint: tuple[Path, CheckpointState] | None,
) -> None:
    if previous_flush is not None:
        previous_flush.result()
    _write_block_file(block_path, block)
    if checkpoint is not None:
        write_checkpoint_file(*checkpoint)
