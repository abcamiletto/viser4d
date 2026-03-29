from __future__ import annotations

import base64
import math
import tempfile
import threading
from collections import OrderedDict
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import msgspec
import numpy as np
import zstandard
from viser import _messages

from ..audio._api import audio_array_payload
from ..audio._messages import AddAudioMessage, is_audio_message
from .._types import StoredMessage, StoredValue
from ._messages_util import (
    TimelineStep,
    extract_message_name,
    is_scene_message,
    serialize_stored_messages,
    store_raw_message,
    stored_dict,
    stored_float,
    stored_int,
)


@dataclass
class TimelineBlock:
    steps: list[TimelineStep]
    dirty: bool = False


@dataclass
class _CheckpointState:
    scene_updates: dict[str, StoredMessage] = field(default_factory=dict)
    key_to_node: dict[str, str | None] = field(default_factory=dict)
    audio_tracks: dict[str, "_AudioTrackState"] = field(default_factory=dict)


@dataclass
class _AudioTrackState:
    sample_rate: int
    waveform: np.ndarray
    volume: float = 1.0


class TimelineStore:
    """Block-backed storage for timeline-owned steps and live scene updates."""

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
        self.block_count = math.ceil(num_steps / block_size)
        self._node_start_steps: dict[str, int] = {}
        self._last_scene_steps: dict[str, int] = {}
        self._lock = threading.RLock()
        self._loaded_blocks: OrderedDict[int, TimelineBlock] = OrderedDict()
        self._max_loaded_blocks = max_loaded_blocks
        self._checkpoint_cache: OrderedDict[int, _CheckpointState] = OrderedDict()
        self._max_cached_checkpoints = max_cached_checkpoints
        self._flush_executor = flush_executor
        self._pending_flushes: dict[int, Future[None]] = {}
        self._block_dir_root = tempfile.TemporaryDirectory(prefix="viser4d-timeline-")
        self._block_dir = Path(self._block_dir_root.name)
        # Running checkpoint for eager incremental persistence during sequential recording.
        self._eager_checkpoint = _CheckpointState()
        self._eager_checkpoint_next_block = 0

    def validate_step(self, step: int) -> int:
        """Return ``step`` if it is in range, else raise ``IndexError``."""
        if step < 0 or step >= self.num_steps:
            raise IndexError(
                f"Timestep {step} is out of range for {self.num_steps} steps."
            )
        return step

    def step(self, step: int) -> TimelineStep:
        """Return the storage bucket for one timestep."""
        with self._lock:
            step = self.validate_step(step)
            block = self._load_block(step // self.block_size)
            return block.steps[step % self.block_size]

    def has_node(self, name: str) -> bool:
        with self._lock:
            return name in self._node_start_steps

    def record_step(self, step: int, messages: list[_messages.Message]) -> None:
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
                    if name is not None and isinstance(
                        message, _messages._CreateSceneNodeMessage
                    ):
                        self._node_start_steps.setdefault(name, step)
                    self._last_scene_steps[key] = step
                    step_state.scene_updates.pop(key, None)
                    step_state.scene_updates[key] = stored_message
                    continue
                if is_audio_message(message):
                    step_state.audio_updates.append(stored_message)
            block.dirty = True
            self._invalidate_checkpoints_after_block(block_index)

    def record_live_scene_update(self, message: _messages.Message) -> int:
        with self._lock:
            stored_message = store_raw_message(message)
            name = extract_message_name(stored_message)
            key = message.redundancy_key()
            start_step = 0 if name is None else self._node_start_steps.get(name, 0)
            last_scene_step = self._last_scene_steps.get(key)
            if last_scene_step is not None:
                start_step = max(start_step, last_scene_step)
            block_index = start_step // self.block_size
            block = self._load_block(block_index)
            step_state = block.steps[start_step % self.block_size]
            step_state.scene_updates.pop(key, None)
            step_state.scene_updates[key] = stored_message
            self._last_scene_steps[key] = start_step
            block.dirty = True
            self._invalidate_checkpoints_after_block(block_index)
            return start_step

    def block_index_for_step(self, step: int) -> int:
        return self.validate_step(step) // self.block_size

    def block_payload(self, block_index: int) -> dict[str, object]:
        with self._lock:
            block_index = self._validate_block_index(block_index)
            checkpoint = self._checkpoint_for_block(block_index)
            block = self._load_block(block_index)
            step_messages = [
                list(step.scene_updates.values()) + list(step.audio_updates)
                for step in block.steps
            ]
        checkpoint_messages = self._checkpoint_messages(checkpoint)
        return {
            "block": block_index,
            "checkpointMessages": serialize_stored_messages(checkpoint_messages),
            "stepMessages": [
                serialize_stored_messages(messages) for messages in step_messages
            ],
        }

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
            payload = msgspec.msgpack.decode(raw)
            scene_steps = cast(
                list[list[tuple[str, StoredMessage]]], payload["sceneSteps"]
            )
            audio_steps = cast(list[list[StoredMessage]], payload["audioSteps"])
            block = TimelineBlock(
                steps=[
                    TimelineStep(
                        scene_updates=dict(scene_updates),
                        audio_updates=list(audio_updates),
                    )
                    for scene_updates, audio_updates in zip(
                        scene_steps,
                        audio_steps,
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
        checkpoint: tuple[Path, _CheckpointState] | None = None
        if (
            block_index == self._eager_checkpoint_next_block
            and next_block < self.block_count
        ):
            for step in block.steps:
                for key, message in step.scene_updates.items():
                    self._apply_scene_message(self._eager_checkpoint, key, message)
                for message in step.audio_updates:
                    self._apply_audio_message(self._eager_checkpoint, message)
            self._eager_checkpoint_next_block = next_block
            checkpoint = (
                self._checkpoint_path(next_block),
                self._copy_checkpoint(self._eager_checkpoint),
            )
        self._pending_flushes[block_index] = self._flush_executor.submit(
            _write_block_and_checkpoint_after,
            pending_flush,
            self._block_path(block_index),
            block,
            checkpoint,
        )

    def _wait_for_pending_flush(self, block_index: int) -> None:
        future = self._pending_flushes.pop(block_index, None)
        if future is not None:
            future.result()

    def _invalidate_checkpoints_after_block(self, block_index: int) -> None:
        stale = [i for i in self._checkpoint_cache if i > block_index]
        for i in stale:
            del self._checkpoint_cache[i]

    def _checkpoint_for_block(self, block_index: int) -> _CheckpointState:
        block_index = self._validate_block_index(block_index)
        cached = self._checkpoint_cache.get(block_index)
        if cached is not None:
            self._checkpoint_cache.move_to_end(block_index)
            return self._copy_checkpoint(cached)
        # Check disk before falling back to block replay.
        disk_state = self._load_checkpoint_from_disk(block_index)
        if disk_state is not None:
            self._cache_checkpoint(block_index, disk_state)
            return disk_state
        prior_blocks = [i for i in self._checkpoint_cache if i < block_index]
        base_index = max(prior_blocks, default=0)
        state = (
            self._copy_checkpoint(self._checkpoint_cache[base_index])
            if base_index in self._checkpoint_cache
            else _CheckpointState()
        )
        for index in range(base_index, block_index):
            self._apply_block_to_checkpoint(state, index)
            self._cache_checkpoint(index + 1, state)
        return self._copy_checkpoint(state)

    def _load_checkpoint_from_disk(self, block_index: int) -> _CheckpointState | None:
        # Wait for any in-flight flush that might be writing this checkpoint.
        if block_index > 0:
            pending = self._pending_flushes.get(block_index - 1)
            if pending is not None:
                pending.result()
        path = self._checkpoint_path(block_index)
        return _load_checkpoint_file(path) if path.exists() else None

    def _cache_checkpoint(self, block_index: int, state: _CheckpointState) -> None:
        self._checkpoint_cache[block_index] = self._copy_checkpoint(state)
        self._checkpoint_cache.move_to_end(block_index)
        while len(self._checkpoint_cache) > self._max_cached_checkpoints:
            self._checkpoint_cache.popitem(last=False)

    def _copy_checkpoint(self, state: _CheckpointState) -> _CheckpointState:
        return _CheckpointState(
            scene_updates=dict(state.scene_updates),
            key_to_node=dict(state.key_to_node),
            audio_tracks={
                name: _AudioTrackState(
                    sample_rate=track.sample_rate,
                    waveform=track.waveform.copy(),
                    volume=track.volume,
                )
                for name, track in state.audio_tracks.items()
            },
        )

    def _apply_block_to_checkpoint(
        self, state: _CheckpointState, block_index: int
    ) -> None:
        block = self._load_block(block_index)
        for step in block.steps:
            for key, message in step.scene_updates.items():
                self._apply_scene_message(state, key, message)
            for message in step.audio_updates:
                self._apply_audio_message(state, message)

    def _apply_scene_message(
        self, state: _CheckpointState, key: str, message: StoredMessage
    ) -> None:
        name = extract_message_name(message)
        if message.get("type") == "RemoveSceneNodeMessage" and isinstance(name, str):
            prefix = f"{name}/"
            stale_keys = [
                k
                for k, node in state.key_to_node.items()
                if node == name or (isinstance(node, str) and node.startswith(prefix))
            ]
            for k in stale_keys:
                del state.scene_updates[k]
                del state.key_to_node[k]
            return
        state.scene_updates.pop(key, None)
        state.scene_updates[key] = message
        state.key_to_node[key] = name

    def _apply_audio_message(
        self, state: _CheckpointState, message: StoredMessage
    ) -> None:
        message_type = message.get("type")
        name = extract_message_name(message)
        if not isinstance(message_type, str) or not isinstance(name, str):
            return
        if message_type == "RemoveAudioMessage":
            state.audio_tracks.pop(name, None)
            return
        if message_type == "AddAudioMessage":
            state.audio_tracks[name] = _AudioTrackState(
                sample_rate=stored_int(message["sampleRate"]),
                waveform=_decode_audio_payload(stored_dict(message["waveform"])),
                volume=stored_float(message["volume"]),
            )
            return
        track = state.audio_tracks.get(name)
        if track is None:
            return
        if message_type == "SetAudioVolumeMessage":
            track.volume = stored_float(message["volume"])
        elif message_type == "SetAudioWaveformMessage":
            track.waveform = _decode_audio_payload(stored_dict(message["waveform"]))
        elif message_type == "AppendAudioMessage":
            track.waveform = _append_audio_waveform(
                track.waveform,
                _decode_audio_payload(stored_dict(message["waveform"])),
            )

    def _checkpoint_messages(self, state: _CheckpointState) -> list[StoredMessage]:
        unnamed_messages = [
            message
            for key, message in state.scene_updates.items()
            if state.key_to_node.get(key) is None
        ]
        node_messages: dict[str, list[StoredMessage]] = {}
        for key, message in state.scene_updates.items():
            name = state.key_to_node.get(key)
            if name is None:
                continue
            node_messages.setdefault(name, []).append(message)
        scene_messages = list(unnamed_messages)
        for name in sorted(node_messages, key=_scene_node_sort_key):
            messages = node_messages[name]
            create_messages = [m for m in messages if _is_create_scene_message(m)]
            scene_messages.extend(create_messages)
            scene_messages.extend(
                m for m in messages if not _is_create_scene_message(m)
            )
        audio_messages = [
            store_raw_message(
                AddAudioMessage(
                    name=name,
                    sampleRate=track.sample_rate,
                    waveform=audio_array_payload(track.waveform),
                    volume=track.volume,
                )
            )
            for name, track in sorted(state.audio_tracks.items())
        ]
        return scene_messages + audio_messages

    def close(self) -> None:
        with self._lock:
            for block_index, block in list(self._loaded_blocks.items()):
                self._flush_block(block_index, block)
            self._loaded_blocks.clear()
            for block_index in tuple(self._pending_flushes):
                self._wait_for_pending_flush(block_index)
            self._block_dir_root.cleanup()


def _scene_node_sort_key(name: str) -> tuple[int, str]:
    return (name.count("/"), name)


def _is_create_scene_message(message: StoredMessage) -> bool:
    return "props" in message


def _decode_audio_payload(payload: dict[str, StoredValue]) -> np.ndarray:
    dtype = np.dtype(str(payload["dtype"]))
    num_channels = stored_int(payload["numChannels"])
    num_frames = stored_int(payload["numFrames"])
    data = np.frombuffer(base64.b64decode(str(payload["data"])), dtype=dtype)
    if num_channels <= 1:
        return np.ascontiguousarray(data[:num_frames])
    return np.ascontiguousarray(data.reshape(num_frames, num_channels))


def _append_audio_waveform(head: np.ndarray, tail: np.ndarray) -> np.ndarray:
    if head.ndim != tail.ndim:
        raise ValueError("Audio append must preserve waveform dimensionality.")
    return np.ascontiguousarray(np.concatenate((head, tail), axis=0))


def _write_block_file(path: Path, block: TimelineBlock) -> None:
    payload = {
        "sceneSteps": [list(step.scene_updates.items()) for step in block.steps],
        "audioSteps": [step.audio_updates for step in block.steps],
    }
    packed = msgspec.msgpack.encode(payload)
    compressed = zstandard.ZstdCompressor(level=6).compress(packed)
    path.write_bytes(compressed)


def _write_checkpoint_file(path: Path, state: _CheckpointState) -> None:
    audio_tracks = [
        {
            "name": name,
            "sampleRate": track.sample_rate,
            "waveform": track.waveform.tobytes(),
            "dtype": track.waveform.dtype.str,
            "shape": list(track.waveform.shape),
            "volume": track.volume,
        }
        for name, track in sorted(state.audio_tracks.items())
    ]
    payload = {
        "sceneUpdates": list(state.scene_updates.items()),
        "keyToNode": list(state.key_to_node.items()),
        "audioTracks": audio_tracks,
    }
    packed = msgspec.msgpack.encode(payload)
    compressed = zstandard.ZstdCompressor(level=6).compress(packed)
    path.write_bytes(compressed)


def _load_checkpoint_file(path: Path) -> _CheckpointState:
    raw = zstandard.ZstdDecompressor().decompress(path.read_bytes())
    payload = msgspec.msgpack.decode(raw)
    scene_updates: dict[str, StoredMessage] = dict(payload["sceneUpdates"])
    key_to_node: dict[str, str | None] = dict(payload["keyToNode"])
    audio_tracks: dict[str, _AudioTrackState] = {}
    for td in payload["audioTracks"]:
        shape = tuple(td["shape"])
        arr = np.frombuffer(td["waveform"], dtype=np.dtype(td["dtype"]))
        audio_tracks[td["name"]] = _AudioTrackState(
            sample_rate=td["sampleRate"],
            waveform=np.ascontiguousarray(arr.reshape(shape)),
            volume=td["volume"],
        )
    return _CheckpointState(
        scene_updates=scene_updates,
        key_to_node=key_to_node,
        audio_tracks=audio_tracks,
    )


def _write_block_and_checkpoint_after(
    previous_flush: Future[None] | None,
    block_path: Path,
    block: TimelineBlock,
    checkpoint: tuple[Path, _CheckpointState] | None,
) -> None:
    if previous_flush is not None:
        previous_flush.result()
    _write_block_file(block_path, block)
    if checkpoint is not None:
        _write_checkpoint_file(*checkpoint)
