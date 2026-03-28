from __future__ import annotations

import base64
import inspect
from dataclasses import dataclass, field
from typing import Any, cast

import msgspec
import numpy as np
import viser
import zstandard
from viser import _messages
from viser.infra import WebsockMessageHandler

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


_CREATE_SCENE_MESSAGE_TYPES = {
    name
    for name, obj in vars(_messages).items()
    if inspect.isclass(obj)
    and issubclass(obj, _messages.Message)
    and obj is not _messages._CreateSceneNodeMessage
    and issubclass(obj, _messages._CreateSceneNodeMessage)
}


def is_create_scene_message(message: StoredMessage) -> bool:
    message_type = message.get("type")
    return isinstance(message_type, str) and message_type in _CREATE_SCENE_MESSAGE_TYPES


@dataclass
class TimelineStep:
    """Recorded scene and audio updates for one timestep."""

    scene_updates: dict[str, StoredMessage] = field(default_factory=dict)
    audio_updates: dict[str, StoredMessage] = field(default_factory=dict)


class TimelineStore:
    """In-memory storage for timeline-owned nodes and per-step message snapshots."""

    def __init__(self, num_steps: int) -> None:
        self.num_steps = num_steps
        self.steps = [TimelineStep() for _ in range(num_steps)]
        self._node_start_steps: dict[str, int] = {}
        self._last_scene_steps: dict[str, int] = {}

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
        return name in self._node_start_steps

    def messages_for_step(self, step: int) -> list[StoredMessage]:
        step_state = self.step(step)
        return list(step_state.scene_updates.values()) + list(
            step_state.audio_updates.values()
        )

    def record_step(self, step: int, messages: list[_messages.Message]) -> None:
        """Store one timestep's scene and audio updates."""
        step_state = self.step(step)
        for message in messages:
            stored_message = store_raw_message(message)
            key = message.redundancy_key()
            name = extract_message_name(stored_message)
            if is_scene_message(stored_message):
                if name is not None and is_create_scene_message(stored_message):
                    self._node_start_steps.setdefault(name, step)
                self._last_scene_steps[key] = step
                self._store_message(step_state.scene_updates, key, stored_message)
                continue
            if name is not None and is_audio_message_type(stored_message.get("type")):
                self._store_message(step_state.audio_updates, key, stored_message)

    def record_live_scene_update(self, message: _messages.Message) -> int:
        stored_message = store_raw_message(message)
        name = extract_message_name(stored_message)
        key = message.redundancy_key()
        start_step = 0 if name is None else self._node_start_steps.get(name, 0)
        last_scene_step = self._last_scene_steps.get(key)
        if last_scene_step is not None:
            start_step = max(start_step, last_scene_step)
        self._store_message(self.step(start_step).scene_updates, key, stored_message)
        self._last_scene_steps[key] = start_step
        return start_step

    def _store_message(
        self,
        bucket: dict[str, Any],
        key: str,
        message: Any,
    ) -> None:
        bucket.pop(key, None)
        bucket[key] = message


class TimelineRecorder(WebsockMessageHandler):
    """Temporary websocket sink used while recording a timestep."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[_messages.Message] = []

    def get_message_buffer(self) -> Any:
        return self

    def push(self, message: _messages.Message) -> None:
        self.messages.append(message)

    def atomic_start(self) -> None:
        return

    def atomic_end(self) -> None:
        return


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
