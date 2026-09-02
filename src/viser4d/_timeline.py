"""Block-backed timeline storage: per-step deltas, disk spill, checkpoints.

The timeline is split into blocks of ``block_size`` steps, each a list of
``StepDelta``. Blocks are LRU-cached in memory and spilled to a temporary
directory (zstd + msgpack) through a dedicated single-worker executor, so writes
per block stay ordered.

Checkpoints are memory-only: a checkpoint for block ``k`` is the folded scene and
audio state of every delta *before* ``k`` (exclusive), so the state at block
offset ``o`` is ``fold(checkpoint, deltas[0..o])``. Staleness has one rule: a
checkpoint built at rev ``r`` is valid iff no earlier block was written after
``r``. A cold seek re-folds the blocks before it; sequential access extends the
previous checkpoint and populates the cache along the way.

Everything here runs under a single ``RLock``: the user thread records while
viser's websocket and event-loop threads read blocks.
"""

from __future__ import annotations

import dataclasses
import math
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

import msgspec
import zstandard

from . import _state, _viser
from ._protocol import TimelineBlockMessage
from ._state import (
    AudioEventRecord,
    AudioState,
    OverrideState,
    SceneEntryRecord,
    SceneState,
    StepDelta,
    StoredMessage,
)

_MAX_LOADED_BLOCKS = 4
_MAX_CHECKPOINTS = 4


@dataclasses.dataclass
class _Block:
    deltas: list[StepDelta]
    dirty: bool = False
    encoded_size: int | None = None


@dataclasses.dataclass
class _Checkpoint:
    scene: SceneState
    audio: AudioState
    built_rev: int


class Timeline:
    """Canonical per-step storage plus the live override overlay."""

    def __init__(self, num_steps: int, *, block_size: int) -> None:
        self.num_steps = num_steps
        self.block_size = block_size
        self._lock = threading.RLock()
        self._rev = 0
        self._loaded: OrderedDict[int, _Block] = OrderedDict()
        self._checkpoints: OrderedDict[int, _Checkpoint] = OrderedDict()
        self._overrides = OverrideState()
        self._flush_executor = ThreadPoolExecutor(max_workers=1)
        self._pending: dict[int, Future[None]] = {}
        self._dir_root = tempfile.TemporaryDirectory(prefix="viser4d-timeline-")
        self._dir = Path(self._dir_root.name)
        self._block_write_rev = [0] * self.block_count
        self._block_bytes: list[int | None] = [None] * self.block_count

    # -- geometry ---------------------------------------------------------

    @property
    def block_count(self) -> int:
        return math.ceil(self.num_steps / self.block_size)

    def validate_step(self, step: int) -> int:
        if step < 0 or step >= self.num_steps:
            raise IndexError(
                f"Timestep {step} is out of range for {self.num_steps} steps."
            )
        return step

    def validate_block(self, index: int) -> int:
        if index < 0 or index >= self.block_count:
            raise IndexError(
                f"Block {index} is out of range for {self.block_count} blocks."
            )
        return index

    def block_index_for_step(self, step: int) -> int:
        return self.validate_step(step) // self.block_size

    def _next_rev(self) -> int:
        self._rev += 1
        return self._rev

    # -- recording --------------------------------------------------------

    def record_step(self, step: int, messages: list[_viser.Message]) -> None:
        with self._lock:
            step = self.validate_step(step)
            index, offset = divmod(step, self.block_size)
            block = self._load_block(index)
            delta = block.deltas[offset]
            for message in messages:
                self._record_message(delta, StoredMessage.capture(message))
            block.dirty = True
            block.encoded_size = None
            # Stamp a fresh rev unconditionally: delete-only edits consume no
            # rev during folding but must still invalidate later checkpoints.
            self._block_write_rev[index] = self._next_rev()
            self._invalidate_block_bytes_from(index)

    def _record_message(self, delta: StepDelta, stored: StoredMessage) -> None:
        if _state.is_audio(stored):
            delta.audio.append(AudioEventRecord(self._next_rev(), stored))
            return
        puts, deletes = _state.scene_puts_deletes(stored)
        for name in deletes:
            delta.fold_delete(name)
        for put in puts:
            delta.fold_put(
                SceneEntryRecord(put.key, self._next_rev(), put.name, put.message)
            )

    def record_override(self, message: _viser.Message) -> list[SceneEntryRecord]:
        with self._lock:
            return self._overrides.apply(StoredMessage.capture(message), self._next_rev)

    def override_items(self) -> list[SceneEntryRecord]:
        with self._lock:
            return self._overrides.items()

    # -- reads ------------------------------------------------------------

    def step_delta(self, step: int) -> StepDelta:
        with self._lock:
            step = self.validate_step(step)
            index, offset = divmod(step, self.block_size)
            return self._load_block(index).deltas[offset]

    def block_bytes(self) -> list[int | None]:
        """Encoded size of every block, by index; ``None`` until first served."""
        with self._lock:
            return list(self._block_bytes)

    def block_message(self, index: int) -> TimelineBlockMessage:
        with self._lock:
            index = self.validate_block(index)
            checkpoint = self._checkpoint_for(index)
            block = self._load_block(index)
            if self._block_bytes[index] is None:
                self._block_bytes[index] = self._byte_size(checkpoint, block)
            return TimelineBlockMessage(
                index=index,
                checkpointScene=[
                    _state.entry_to_wire(e) for e in checkpoint.scene.entries.values()
                ],
                checkpointAudio=[
                    _state.audio_track_to_wire(name, track)
                    for name, track in sorted(checkpoint.audio.tracks.items())
                ],
                deltas=[_state.delta_to_wire(d) for d in block.deltas],
            )

    # -- checkpoints ------------------------------------------------------

    def _checkpoint_for(self, index: int) -> _Checkpoint:
        if index == 0:
            return _Checkpoint(SceneState(), AudioState(), self._rev)
        # prefix[k] = max write rev among blocks < k; a checkpoint for block k
        # is valid iff it was built at rev >= prefix[k].
        prefix = [0] * (index + 1)
        for i in range(index):
            prefix[i + 1] = max(prefix[i], self._block_write_rev[i])
        cached = self._checkpoints.get(index)
        if cached is not None and cached.built_rev >= prefix[index]:
            self._checkpoints.move_to_end(index)
            return cached

        base = 0
        scene, audio = SceneState(), AudioState()
        for candidate in range(index - 1, 0, -1):
            snapshot = self._checkpoints.get(candidate)
            if snapshot is not None and snapshot.built_rev >= prefix[candidate]:
                scene, audio = snapshot.scene.copy(), snapshot.audio.copy()
                base = candidate
                break

        for idx in range(base, index):
            block = self._load_block(idx)
            base_step = idx * self.block_size
            for offset, delta in enumerate(block.deltas):
                scene.apply_delta(delta)
                for event in delta.audio:
                    audio.apply(event, base_step + offset)
            self._cache_checkpoint(idx + 1, scene, audio)
        return self._checkpoints[index]

    def _cache_checkpoint(
        self, index: int, scene: SceneState, audio: AudioState
    ) -> None:
        self._checkpoints[index] = _Checkpoint(scene.copy(), audio.copy(), self._rev)
        self._checkpoints.move_to_end(index)
        while len(self._checkpoints) > _MAX_CHECKPOINTS:
            self._checkpoints.popitem(last=False)

    def _byte_size(self, checkpoint: _Checkpoint, block: _Block) -> int:
        if block.encoded_size is None:
            block.encoded_size = len(msgspec.msgpack.encode(block.deltas))
        size = block.encoded_size
        for entry in checkpoint.scene.entries.values():
            size += len(msgspec.msgpack.encode(entry.message.payload))
            size += sum(len(b) for b in entry.message.buffers)
        for track in checkpoint.audio.tracks.values():
            size += track.data.nbytes + 64
        return size

    # -- block cache / disk ----------------------------------------------

    def _load_block(self, index: int) -> _Block:
        index = self.validate_block(index)
        block = self._loaded.get(index)
        if block is not None:
            self._loaded.move_to_end(index)
            return block
        self._wait_flush(index)
        path = self._block_path(index)
        if path.exists():
            raw = zstandard.ZstdDecompressor().decompress(path.read_bytes())
            deltas = msgspec.msgpack.decode(raw, type=list[StepDelta])
            block = _Block(deltas, encoded_size=len(raw))
        else:
            count = min(self.block_size, self.num_steps - index * self.block_size)
            block = _Block([StepDelta() for _ in range(count)])
        self._loaded[index] = block
        while len(self._loaded) > _MAX_LOADED_BLOCKS:
            evicted_index, evicted = self._loaded.popitem(last=False)
            self._flush(evicted_index, evicted)
        return block

    def _flush(self, index: int, block: _Block) -> None:
        if not block.dirty:
            return
        block.dirty = False
        self._pending[index] = self._flush_executor.submit(
            _write_block, self._block_path(index), block.deltas
        )

    def _wait_flush(self, index: int) -> None:
        future = self._pending.pop(index, None)
        if future is not None:
            future.result()

    def _block_path(self, index: int) -> Path:
        return self._dir / f"{index:08d}.msgpack.zst"

    def _invalidate_block_bytes_from(self, index: int) -> None:
        for i in range(index, self.block_count):
            self._block_bytes[i] = None

    # -- resize / clear / close ------------------------------------------

    def resize(self, num_steps: int) -> None:
        with self._lock:
            keep = min(self.num_steps, num_steps)
            retained = {step: self.step_delta(step) for step in range(keep)}
            self._reset_storage()
            self.num_steps = num_steps
            self._block_write_rev = [0] * self.block_count
            self._block_bytes = [None] * self.block_count
            for step, delta in retained.items():
                if delta.is_empty():
                    continue
                index, offset = divmod(step, self.block_size)
                block = self._load_block(index)
                block.deltas[offset] = delta
                block.dirty = True
                block.encoded_size = None
                self._block_write_rev[index] = self._rev

    def clear(self) -> None:
        with self._lock:
            self._reset_storage()
            self._overrides.clear()
            self._rev = 0
            self._block_write_rev = [0] * self.block_count
            self._block_bytes = [None] * self.block_count

    def _reset_storage(self) -> None:
        self._wait_all_flushes()
        for path in self._dir.glob("*.msgpack.zst"):
            path.unlink()
        self._loaded.clear()
        self._checkpoints.clear()

    def _wait_all_flushes(self) -> None:
        for index in tuple(self._pending):
            self._wait_flush(index)

    def close(self) -> None:
        with self._lock:
            for index, block in list(self._loaded.items()):
                self._flush(index, block)
            self._loaded.clear()
            self._wait_all_flushes()
        self._flush_executor.shutdown(wait=True)
        self._dir_root.cleanup()


def _write_block(path: Path, deltas: list[StepDelta]) -> None:
    raw = msgspec.msgpack.encode(deltas)
    path.write_bytes(zstandard.ZstdCompressor(level=6).compress(raw))
