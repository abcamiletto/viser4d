"""Checkpoint state model and all logic for building, applying, and persisting checkpoints."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from pathlib import Path

import msgspec
import numpy as np
import zstandard

from ..audio._api import audio_array_payload
from ..audio._messages import AddAudioMessage
from .._types import StoredMessage
from ._messages_util import (
    TimelineStep,
    extract_message_name,
    store_raw_message,
    stored_dict,
    stored_float,
    stored_int,
)


@dataclass
class AudioTrackState:
    sample_rate: int
    waveform: np.ndarray
    volume: float = 1.0


@dataclass
class CheckpointState:
    scene_updates: dict[str, StoredMessage] = field(default_factory=dict)
    key_to_node: dict[str, str | None] = field(default_factory=dict)
    audio_tracks: dict[str, AudioTrackState] = field(default_factory=dict)


class _CheckpointFilePayload(msgspec.Struct):
    sceneUpdates: list[tuple[str, StoredMessage]]
    keyToNode: list[tuple[str, str | None]]
    audioTracks: list["_CheckpointAudioTrackPayload"]


class _CheckpointAudioTrackPayload(msgspec.Struct):
    name: str
    sampleRate: int
    waveform: bytes
    dtype: str
    shape: list[int]
    volume: float


def apply_scene_message(
    state: CheckpointState, key: str, message: StoredMessage
) -> None:
    name = extract_message_name(message)
    if message.payload.get("type") == "RemoveSceneNodeMessage" and isinstance(
        name, str
    ):
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


def apply_audio_message(state: CheckpointState, message: StoredMessage) -> None:
    message_type = message.payload.get("type")
    name = extract_message_name(message)
    if not isinstance(message_type, str) or not isinstance(name, str):
        return
    if message_type == "RemoveAudioMessage":
        state.audio_tracks.pop(name, None)
        return
    if message_type == "AddAudioMessage":
        state.audio_tracks[name] = AudioTrackState(
            sample_rate=stored_int(message.payload["sampleRate"]),
            waveform=_decode_audio_payload(stored_dict(message.payload["waveform"])),
            volume=stored_float(message.payload["volume"]),
        )
        return
    track = state.audio_tracks.get(name)
    if track is None:
        return
    if message_type == "SetAudioVolumeMessage":
        track.volume = stored_float(message.payload["volume"])
    elif message_type == "SetAudioWaveformMessage":
        track.waveform = _decode_audio_payload(stored_dict(message.payload["waveform"]))
    elif message_type == "AppendAudioMessage":
        track.waveform = _append_audio_waveform(
            track.waveform,
            _decode_audio_payload(stored_dict(message.payload["waveform"])),
        )


def apply_steps(state: CheckpointState, steps: list[TimelineStep]) -> None:
    """Apply all scene and audio updates from a sequence of steps."""
    for step in steps:
        for key, message in step.scene_updates.items():
            apply_scene_message(state, key, message)
        for message in step.audio_updates:
            apply_audio_message(state, message)


def copy_checkpoint(state: CheckpointState) -> CheckpointState:
    return CheckpointState(
        scene_updates=dict(state.scene_updates),
        key_to_node=dict(state.key_to_node),
        audio_tracks={
            name: AudioTrackState(
                sample_rate=track.sample_rate,
                waveform=track.waveform.copy(),
                volume=track.volume,
            )
            for name, track in state.audio_tracks.items()
        },
    )


def checkpoint_messages(state: CheckpointState) -> list[StoredMessage]:
    """Build the ordered message list that reconstructs this checkpoint's state."""
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
        create = [m for m in messages if _is_create_scene_message(m)]
        scene_messages.extend(create)
        scene_messages.extend(m for m in messages if not _is_create_scene_message(m))
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


def write_checkpoint_file(path: Path, state: CheckpointState) -> None:
    audio_tracks = [
        _CheckpointAudioTrackPayload(
            name=name,
            sampleRate=track.sample_rate,
            waveform=track.waveform.tobytes(),
            dtype=track.waveform.dtype.str,
            shape=list(track.waveform.shape),
            volume=track.volume,
        )
        for name, track in sorted(state.audio_tracks.items())
    ]
    payload = _CheckpointFilePayload(
        sceneUpdates=list(state.scene_updates.items()),
        keyToNode=list(state.key_to_node.items()),
        audioTracks=audio_tracks,
    )
    packed = msgspec.msgpack.encode(payload)
    compressed = zstandard.ZstdCompressor(level=6).compress(packed)
    path.write_bytes(compressed)


def load_checkpoint_file(path: Path) -> CheckpointState:
    raw = zstandard.ZstdDecompressor().decompress(path.read_bytes())
    payload = msgspec.msgpack.decode(raw, type=_CheckpointFilePayload)
    scene_updates = dict(payload.sceneUpdates)
    key_to_node = dict(payload.keyToNode)
    audio_tracks: dict[str, AudioTrackState] = {}
    for td in payload.audioTracks:
        shape = tuple(td.shape)
        arr = np.frombuffer(td.waveform, dtype=np.dtype(td.dtype))
        audio_tracks[td.name] = AudioTrackState(
            sample_rate=td.sampleRate,
            waveform=np.ascontiguousarray(arr.reshape(shape)),
            volume=td.volume,
        )
    return CheckpointState(
        scene_updates=scene_updates,
        key_to_node=key_to_node,
        audio_tracks=audio_tracks,
    )


def _scene_node_sort_key(name: str) -> tuple[int, str]:
    return (name.count("/"), name)


def _is_create_scene_message(message: StoredMessage) -> bool:
    return "props" in message.payload


def _decode_audio_payload(payload: dict[str, object]) -> np.ndarray:
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
