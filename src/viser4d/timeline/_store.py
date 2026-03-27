from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, cast

import msgspec
import numpy as np
import viser
import zstandard
from viser import _messages

from ..audio._messages import is_audio_message_type
from .._types import (
    BinaryPayload,
    JSONValue,
    SerializedMessage,
    StoredMessage,
    StoredValue,
)


def to_stored(value: Any) -> StoredValue:
    """Convert a viser's serializable payload into the timeline storage form."""
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, np.ndarray):
        return value.tobytes()
    if isinstance(value, dict):
        return {str(key): to_stored(val) for key, val in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_stored(item) for item in value]
    return cast(StoredValue, value)


def to_jsonable(value: StoredValue) -> JSONValue:
    """Convert stored timeline data into the JSON-safe runtime transport form."""
    if isinstance(value, (bytes, bytearray)):
        return cast(
            JSONValue,
            BinaryPayload(
                __viser4d_binary__=base64.b64encode(bytes(value)).decode("ascii")
            ),
        )
    if isinstance(value, dict):
        return {str(key): to_jsonable(val) for key, val in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_jsonable(item) for item in value]
    return cast(JSONValue, value)


def store_raw_message(message: _messages.Message) -> StoredMessage:
    """Capture one viser message in the timeline's canonical storage form."""
    return cast(StoredMessage, to_stored(message.as_serializable_dict()))


def store_raw_messages(messages: list[_messages.Message]) -> list[StoredMessage]:
    return [store_raw_message(message) for message in messages]


def serialize_stored_message(message: StoredMessage) -> SerializedMessage:
    return cast(SerializedMessage, to_jsonable(message))


def serialize_stored_messages(messages: list[StoredMessage]) -> list[SerializedMessage]:
    return [serialize_stored_message(message) for message in messages]


def extract_message_name(message: StoredMessage) -> str | None:
    name = message.get("name")
    return name if isinstance(name, str) and name else None


def is_scene_message(message: StoredMessage) -> bool:
    message_type = message.get("type")
    return (
        isinstance(message_type, str)
        and not message_type.startswith("Gui")
        and not is_audio_message_type(message_type)
    )


@dataclass
class TimelineStep:
    """Recorded scene and audio updates for one timestep."""

    scene_updates: list["TimelineUpdate"] = field(default_factory=list)
    audio_updates: list["TimelineUpdate"] = field(default_factory=list)

    @property
    def node_names(self) -> set[str]:
        return {update.name for update in self.scene_updates}


@dataclass
class TimelineUpdate:
    """One ordered update attached to a named timeline object."""

    name: str
    message: StoredMessage


class TimelineStore:
    """In-memory storage for timestep messages and timeline-owned node names."""

    def __init__(self, num_steps: int) -> None:
        self.num_steps = num_steps
        self.steps = [TimelineStep() for _ in range(num_steps)]
        self.node_names: set[str] = set()
        self.live_scene_updates: dict[str, StoredMessage] = {}

    def validate_step(self, step: int) -> int:
        """Return ``step`` if it is in range, else raise ``IndexError``."""
        if step < 0 or step >= self.num_steps:
            raise IndexError(
                f"Timestep {step} is out of range for {self.num_steps} steps."
            )
        return step

    def step(self, step: int) -> TimelineStep:
        """Return the storage bucket for one timestep."""
        return self.steps[self.validate_step(step)]

    def has_node(self, name: str) -> bool:
        return name in self.node_names

    def record_step(self, step: int, messages: list[StoredMessage]) -> TimelineStep:
        """Store one timestep's scene and audio updates."""
        step_state = self.step(step)
        for message in messages:
            if is_scene_message(message):
                self._record_scene_message(step_state, message)
                continue
            if is_audio_message_type(message.get("type")):
                track_name = extract_message_name(message)
                if track_name is None:
                    continue
                self._record_audio_message(step_state, track_name, message)
        return step_state

    def record_live_scene_update(
        self,
        message: StoredMessage,
        *,
        redundancy_key: str,
    ) -> None:
        """Store the latest live scene overlay keyed by viser's redundancy key."""
        clear_node_name = (
            extract_message_name(message)
            if message.get("type") == "RemoveSceneNodeMessage"
            else None
        )
        if clear_node_name is not None:
            for key, update in list(self.live_scene_updates.items()):
                if extract_message_name(update) == clear_node_name:
                    self.live_scene_updates.pop(key)
        self.live_scene_updates.pop(redundancy_key, None)
        self.live_scene_updates[redundancy_key] = message

    def iter_live_scene_updates(self) -> tuple[tuple[str, StoredMessage], ...]:
        """Return the current live scene overlay snapshot in replay order."""
        return tuple(self.live_scene_updates.items())

    def _record_audio_message(
        self,
        step_state: TimelineStep,
        track_name: str,
        message: StoredMessage,
    ) -> None:
        step_state.audio_updates.append(TimelineUpdate(track_name, message))

    def _record_scene_message(
        self,
        step_state: TimelineStep,
        message: StoredMessage,
    ) -> None:
        node_name = extract_message_name(message)
        if node_name is None:
            return
        self.node_names.add(node_name)
        step_state.scene_updates.append(TimelineUpdate(node_name, message))


def serialize_viser_recording(
    messages: list[tuple[float, StoredMessage]],
    *,
    duration_seconds: float = 0.0,
) -> bytes:
    packed = msgspec.msgpack.encode(
        {
            "durationSeconds": duration_seconds,
            "messages": messages,
            "viserVersion": viser.__version__,
        }
    )
    compressed = zstandard.ZstdCompressor(level=12).compress(packed)
    return len(packed).to_bytes(8, "little") + compressed
