"""Block-backed timeline storage with LRU caching and checkpoint management."""

from __future__ import annotations

import math
import tempfile
import threading
from collections.abc import Sequence
from collections import OrderedDict
from concurrent.futures import Executor, Future
from dataclasses import dataclass
from pathlib import Path

import msgspec
import zstandard

from .. import _viser_private as impl
from .._types import RuntimeBlockPayload, StoredMessage, StoredStatePatch
from ._checkpoint import (
    CheckpointState,
    CheckpointSnapshot,
    apply_audio_message,
    apply_scene_message,
    apply_steps,
    checkpoint_audio_messages,
    checkpoint_scene_entries,
    copy_checkpoint,
    load_checkpoint_file,
    remove_scene_node_subtree,
    step_patch_messages,
    write_checkpoint_file,
)
from ._messages_util import (
    TimelineStep,
    extract_message_name,
    is_scene_message,
    record_scene_delete,
    record_scene_message,
    scene_delete_state_key,
    scene_entries_for_message,
    step_patch_payload,
    store_raw_message,
    timeline_step_from_patch_payload,
)
from ._streaming import BlockManifest


@dataclass
class TimelineBlock:
    """In-memory representation of one block of recorded timesteps."""

    steps: list[TimelineStep]
    dirty: bool = False


@dataclass
class _BlockManifestState:
    payload_byte_size: int | None = None
    dirty: bool = True


class _BlockFilePayload(msgspec.Struct):
    stepPatches: list[StoredStatePatch]


class TimelineStore:
    """Block-backed storage for timeline-owned steps and live scene overrides."""

    def __init__(
        self,
        num_steps: int,
        *,
        block_size: int = 32,
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
        self._scene_overrides = TimelineStep()
        self._lock = threading.RLock()
        self._loaded_blocks: OrderedDict[int, TimelineBlock] = OrderedDict()
        self._max_loaded_blocks = max_loaded_blocks
        self._checkpoint_cache: OrderedDict[int, CheckpointSnapshot] = OrderedDict()
        self._max_cached_checkpoints = max_cached_checkpoints
        self._manifest_states = [_BlockManifestState() for _ in range(self.block_count)]
        self._flush_executor = flush_executor
        self._pending_flushes: dict[int, Future[None]] = {}
        self._block_dir_root = tempfile.TemporaryDirectory(prefix="viser4d-timeline-")
        self._block_dir = Path(self._block_dir_root.name)
        # Each checkpoint boundary stores the latest revision that contributes
        # to it, so cached checkpoints can be validated lazily.
        self._checkpoint_revisions = [0] * self.block_count
        self._next_revision = 1

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

    def record_step(self, step: int, messages: list[impl.Message]) -> None:
        """Store one timestep's scene and audio updates."""
        with self._lock:
            step = self.validate_step(step)
            block_index = step // self.block_size
            step_offset = step % self.block_size
            before_step = self._checkpoint_for_block(block_index, persist=False)
            block = self._load_block(block_index)
            apply_steps(before_step, block.steps[:step_offset])
            existing_messages = step_patch_messages(block.steps[step_offset])
            block.steps[step_offset] = self._compile_step_patch(
                before_step,
                [
                    *existing_messages,
                    *(store_raw_message(message) for message in messages),
                ],
            )
            block.dirty = True
            revision = self._next_revision
            self._next_revision += 1
            for checkpoint_index in range(block_index + 1, self.block_count):
                self._checkpoint_revisions[checkpoint_index] = revision
            self._invalidate_manifests_after_block(block_index)

    def record_scene_override(self, message: StoredMessage) -> None:
        """Store one live scene override for playback replay and export."""
        with self._lock:
            record_scene_message(self._scene_overrides, message)

    def scene_override_items(self) -> tuple[tuple[str, StoredMessage], ...]:
        """Return keyed live scene overrides in client replay order."""
        with self._lock:
            delete_items = [
                (
                    scene_delete_state_key(node_name),
                    StoredMessage(
                        payload={"type": "RemoveSceneNodeMessage", "name": node_name}
                    ),
                )
                for node_name in self._scene_overrides.scene_delete_nodes
            ]
            return tuple(delete_items) + tuple(self._scene_overrides.scene_puts.items())

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
                    src = source_block.steps[offset]
                    target_block.steps[offset] = TimelineStep(
                        scene_puts=dict(src.scene_puts),
                        scene_delete_nodes=list(src.scene_delete_nodes),
                        audio_messages=list(src.audio_messages),
                    )

            resized._scene_overrides = TimelineStep(
                scene_puts=dict(self._scene_overrides.scene_puts),
                scene_delete_nodes=list(self._scene_overrides.scene_delete_nodes),
            )

            return resized

    def _compile_step_patch(
        self,
        state: CheckpointState,
        messages: list[StoredMessage],
    ) -> TimelineStep:
        step = TimelineStep()
        for stored_message in messages:
            if is_scene_message(stored_message):
                self._compile_scene_message(step, state, stored_message)
                continue
            step.audio_messages.append(stored_message)
            apply_audio_message(state, stored_message)
        return step

    def _compile_scene_message(
        self,
        step: TimelineStep,
        state: CheckpointState,
        stored_message: StoredMessage,
    ) -> None:
        scene_puts, scene_delete_nodes = scene_entries_for_message(stored_message)
        for node_name in scene_delete_nodes:
            record_scene_delete(step, node_name)
            remove_scene_node_subtree(state, node_name)

        node_name = extract_message_name(stored_message)
        recreates_existing_node = (
            "props" in stored_message.payload
            and isinstance(node_name, str)
            and any(
                visible_node == node_name or visible_node.startswith(f"{node_name}/")
                for visible_node in state.key_to_node.values()
                if isinstance(visible_node, str)
            )
            and node_name not in step.scene_delete_nodes
        )
        if recreates_existing_node:
            assert node_name is not None
            record_scene_delete(step, node_name)
            remove_scene_node_subtree(state, node_name)

        for entry in scene_puts:
            step.scene_puts[entry["key"]] = entry["message"]
            apply_scene_message(state, entry["key"], entry["message"])

    def messages_for_step(self, step: int) -> list[StoredMessage]:
        """Return the recorded step delta plus visible live scene overrides."""
        with self._lock:
            step = self.validate_step(step)
            block_index = step // self.block_size
            step_offset = step % self.block_size
            state = self._checkpoint_for_block(block_index, persist=False)
            block = self._load_block(block_index)
            apply_steps(state, block.steps[: step_offset + 1])
            return [
                *step_patch_messages(block.steps[step_offset]),
                *step_patch_messages(self._visible_scene_overrides_for_state(state)),
            ]

    def block_index_for_step(self, step: int) -> int:
        return self.validate_step(step) // self.block_size

    def block_manifests(self) -> tuple[BlockManifest, ...]:
        with self._lock:
            return tuple(
                self._block_manifest_locked(block_index)
                for block_index in range(self.block_count)
            )

    def block_payload(self, block_index: int) -> RuntimeBlockPayload:
        """Build the keyed checkpoint-plus-step-patches payload for the browser."""
        with self._lock:
            block_index = self._validate_block_index(block_index)
            ckpt = self._checkpoint_for_block(block_index)
            block = self._load_block(block_index)
            checkpoint_scene = checkpoint_scene_entries(ckpt)
            checkpoint_audio = checkpoint_audio_messages(ckpt)
            step_patches = [step_patch_payload(step) for step in block.steps]
            manifest = self._manifest_states[block_index]
            manifest.payload_byte_size = _block_payload_byte_size(
                checkpoint_scene,
                checkpoint_audio,
                step_patches,
            )
            manifest.dirty = False
        return {
            "block": block_index,
            "checkpointSceneEntries": checkpoint_scene,
            "checkpointAudioMessages": checkpoint_audio,
            "stepPatches": step_patches,
        }

    def _visible_scene_overrides_for_state(
        self, state: CheckpointState
    ) -> TimelineStep:
        visible_nodes = {
            node_name
            for node_name in state.key_to_node.values()
            if isinstance(node_name, str)
        }
        return TimelineStep(
            scene_puts={
                key: message
                for key, message in self._scene_overrides.scene_puts.items()
                if (
                    (node_name := extract_message_name(message)) is None
                    or node_name in visible_nodes
                )
            },
            scene_delete_nodes=[
                node_name
                for node_name in self._scene_overrides.scene_delete_nodes
                if any(
                    node_name == visible_name
                    or visible_name.startswith(f"{node_name}/")
                    for visible_name in visible_nodes
                )
            ],
        )

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
                    timeline_step_from_patch_payload(patch)
                    for patch in payload.stepPatches
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
        self._pending_flushes[block_index] = self._flush_executor.submit(
            _write_block_after,
            pending_flush,
            self._block_path(block_index),
            block,
        )

    def _wait_for_pending_flush(self, block_index: int) -> None:
        future = self._pending_flushes.pop(block_index, None)
        if future is not None:
            future.result()

    def _invalidate_manifests_after_block(self, block_index: int) -> None:
        for manifest in self._manifest_states[block_index:]:
            manifest.payload_byte_size = None
            manifest.dirty = True

    def _checkpoint_for_block(
        self,
        block_index: int,
        *,
        persist: bool = True,
    ) -> CheckpointState:
        block_index = self._validate_block_index(block_index)
        if block_index == 0:
            return CheckpointState()
        snapshot = self._checkpoint_snapshot(block_index)
        if snapshot is not None:
            if persist and not self._checkpoint_path(block_index).exists():
                self._persist_checkpoint_snapshot(block_index, snapshot)
            return copy_checkpoint(snapshot.state)

        base_index = 0
        state = CheckpointState()
        source_revision = 0
        for candidate in range(block_index - 1, 0, -1):
            snapshot = self._checkpoint_snapshot(candidate)
            if snapshot is None:
                continue
            state = copy_checkpoint(snapshot.state)
            source_revision = snapshot.source_revision
            base_index = candidate
            break

        for index in range(base_index, block_index):
            block = self._load_block(index)
            apply_steps(state, block.steps)
            source_revision = self._checkpoint_revisions[index + 1]
            self._cache_checkpoint(index + 1, state, source_revision)

        snapshot = self._checkpoint_cache[block_index]
        if persist:
            self._persist_checkpoint_snapshot(block_index, snapshot)
        return copy_checkpoint(snapshot.state)

    def _checkpoint_snapshot(self, block_index: int) -> CheckpointSnapshot | None:
        expected_source_revision = self._checkpoint_revisions[block_index]
        cached = self._checkpoint_cache.get(block_index)
        if cached is not None and cached.source_revision == expected_source_revision:
            self._checkpoint_cache.move_to_end(block_index)
            return cached
        snapshot = self._load_checkpoint_from_disk(
            block_index,
            expected_source_revision,
        )
        if snapshot is None:
            return None
        self._cache_checkpoint(
            block_index,
            snapshot.state,
            snapshot.source_revision,
        )
        return self._checkpoint_cache[block_index]

    def _load_checkpoint_from_disk(
        self,
        block_index: int,
        expected_source_revision: int,
    ) -> CheckpointSnapshot | None:
        if block_index > 0:
            pending = self._pending_flushes.get(block_index - 1)
            if pending is not None:
                pending.result()
        path = self._checkpoint_path(block_index)
        if not path.exists():
            return None
        snapshot = load_checkpoint_file(path)
        if snapshot.source_revision != expected_source_revision:
            return None
        return snapshot

    def _cache_checkpoint(
        self,
        block_index: int,
        state: CheckpointState,
        source_revision: int,
    ) -> None:
        self._checkpoint_cache[block_index] = CheckpointSnapshot(
            state=copy_checkpoint(state),
            source_revision=source_revision,
        )
        self._checkpoint_cache.move_to_end(block_index)
        while len(self._checkpoint_cache) > self._max_cached_checkpoints:
            self._checkpoint_cache.popitem(last=False)

    def _persist_checkpoint_snapshot(
        self,
        block_index: int,
        snapshot: CheckpointSnapshot,
    ) -> None:
        if block_index == 0:
            return
        write_checkpoint_file(
            self._checkpoint_path(block_index),
            snapshot.state,
            snapshot.source_revision,
        )

    def _block_manifest_locked(self, block_index: int) -> BlockManifest:
        manifest = self._manifest_states[block_index]
        step_start = block_index * self.block_size
        step_stop = min(step_start + self.block_size, self.num_steps)
        return BlockManifest(
            block_index=block_index,
            step_start=step_start,
            step_stop=step_stop,
            payload_byte_size=manifest.payload_byte_size,
            dirty=manifest.dirty,
        )


def _write_block_file(path: Path, block: TimelineBlock) -> None:
    payload = _BlockFilePayload(
        stepPatches=[step_patch_payload(step) for step in block.steps],
    )
    packed = msgspec.msgpack.encode(payload)
    compressed = zstandard.ZstdCompressor(level=6).compress(packed)
    path.write_bytes(compressed)


def _write_block_after(
    previous_flush: Future[None] | None,
    block_path: Path,
    block: TimelineBlock,
) -> None:
    if previous_flush is not None:
        previous_flush.result()
    _write_block_file(block_path, block)


def _block_payload_byte_size(
    scene_entries: Sequence[object],
    audio_messages: Sequence[object],
    step_patches: Sequence[object],
) -> int:
    return len(
        msgspec.msgpack.encode(
            {
                "checkpointSceneEntries": scene_entries,
                "checkpointAudioMessages": audio_messages,
                "stepPatches": step_patches,
            }
        )
    )
