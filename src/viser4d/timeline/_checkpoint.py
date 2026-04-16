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
from .._types import StoredMessage, StoredMessageEntry, StoredPayload
from ._messages_util import (
    TimelineStep,
    extract_message_name,
    step_patch_payload,
    store_raw_message,
    stored_dict,
    stored_float,
    stored_int,
)


@dataclass
class AudioTrackState:
    """Checkpoint snapshot for one logical audio track."""

    sample_rate: int
    waveform: np.ndarray
    volume: float = 1.0


@dataclass
class CheckpointState:
    """Materialized scene and audio state at a block boundary."""

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
    """Fold one stored scene message into a mutable checkpoint."""
    name = extract_message_name(message)
    if message.payload.get("type") == "RemoveSceneNodeMessage" and isinstance(
        name, str
    ):
        remove_scene_node_subtree(state, name)
        return
    state.scene_updates.pop(key, None)
    state.scene_updates[key] = message
    state.key_to_node[key] = name


def remove_scene_node_subtree(state: CheckpointState, node_name: str) -> None:
    prefix = f"{node_name}/"
    stale_keys = [
        key
        for key, scene_node in state.key_to_node.items()
        if scene_node == node_name
        or (isinstance(scene_node, str) and scene_node.startswith(prefix))
    ]
    for key in stale_keys:
        del state.scene_updates[key]
        del state.key_to_node[key]


def apply_audio_message(state: CheckpointState, message: StoredMessage) -> None:
    """Fold one stored audio message into a mutable checkpoint."""
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
        return
    if message_type == "SetAudioWaveformMessage":
        track.waveform = _decode_audio_payload(stored_dict(message.payload["waveform"]))
        return
    if message_type == "AppendAudioMessage":
        track.waveform = _append_audio_waveform(
            track.waveform,
            _decode_audio_payload(stored_dict(message.payload["waveform"])),
        )


def apply_steps(state: CheckpointState, steps: list[TimelineStep]) -> None:
    """Apply all scene and audio updates from a sequence of steps."""
    for step in steps:
        for node_name in step.scene_delete_nodes:
            remove_scene_node_subtree(state, node_name)
        for key, message in step.scene_puts.items():
            apply_scene_message(state, key, message)
        for message in step.audio_messages:
            apply_audio_message(state, message)


def copy_checkpoint(state: CheckpointState) -> CheckpointState:
    """Deep-copy checkpoint state for caching or reuse."""
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


def checkpoint_scene_entries(state: CheckpointState) -> list[StoredMessageEntry]:
    """Build the keyed scene entries for one checkpoint."""
    return [
        {
            "key": key,
            "message": message,
        }
        for key, message in state.scene_updates.items()
    ]


def checkpoint_audio_messages(state: CheckpointState) -> list[StoredMessage]:
    """Build checkpoint audio tracks as add-audio messages."""
    return [
        audio_track_message(name, track)
        for name, track in sorted(state.audio_tracks.items())
    ]


def step_patch_messages(step: TimelineStep) -> list[StoredMessage]:
    """Materialize one step patch back into ordered viewer messages."""
    scene_messages = [
        StoredMessage(
            payload={
                "type": "RemoveSceneNodeMessage",
                "name": node_name,
            }
        )
        for node_name in step.scene_delete_nodes
    ]
    scene_messages.extend(materialize_scene_puts(step.scene_puts))
    return scene_messages + list(step.audio_messages)


def step_patch_payloads(steps: list[TimelineStep]) -> list[StoredPayload]:
    return [step_patch_payload(step) for step in steps]


def write_checkpoint_file(path: Path, state: CheckpointState) -> None:
    """Persist a checkpoint snapshot to disk."""
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
    """Load a checkpoint snapshot written by :func:`write_checkpoint_file`."""
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


def materialize_scene_puts(scene_puts: dict[str, StoredMessage]) -> list[StoredMessage]:
    scene_messages: list[StoredMessage] = []
    node_messages: dict[str, list[StoredMessage]] = {}
    for key, message in scene_puts.items():
        if key.startswith("scene.node:"):
            node_name = key.removeprefix("scene.node:").partition(":")[0]
            node_messages.setdefault(node_name, []).append(message)
            continue
        if key.startswith("scene.root:"):
            node_messages.setdefault("", []).append(message)
            continue
        scene_messages.append(message)

    for name in sorted(node_messages, key=lambda value: (value.count("/"), value)):
        messages = node_messages[name]
        scene_messages.extend(message for message in messages if "props" in message.payload)
        scene_messages.extend(
            message for message in messages if "props" not in message.payload
        )
    return scene_messages


def audio_track_message(name: str, track: AudioTrackState) -> StoredMessage:
    return store_raw_message(
        AddAudioMessage(
            name=name,
            sampleRate=track.sample_rate,
            waveform=audio_array_payload(track.waveform),
            volume=track.volume,
        )
    )


def _decode_audio_payload(payload: StoredPayload) -> np.ndarray:
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
