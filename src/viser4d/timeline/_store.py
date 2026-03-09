from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Callable, cast

import msgspec
import numpy as np
import viser
import zstandard
from viser import _messages
from viser.infra import WebsockMessageHandler

from ..audio._messages import is_audio_message
from .._types import BinaryPayload, JSONValue, SerializedMessage


def to_jsonable(value: Any) -> JSONValue:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return cast(
            JSONValue,
            BinaryPayload(
                __viser4d_binary__=base64.b64encode(bytes(value)).decode("ascii")
            ),
        )
    if isinstance(value, np.ndarray):
        return cast(
            JSONValue,
            BinaryPayload(
                __viser4d_binary__=base64.b64encode(value.tobytes()).decode("ascii")
            ),
        )
    if isinstance(value, dict):
        return {str(key): to_jsonable(val) for key, val in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_jsonable(item) for item in value]
    return value


def serialize_message(message: _messages.Message) -> SerializedMessage:
    return cast(SerializedMessage, to_jsonable(message.as_serializable_dict()))


def extract_node_names(message: _messages.Message) -> set[str]:
    name = getattr(message, "name", None)
    return {name} if isinstance(name, str) and name else set()


def is_scene_message(message: Any) -> bool:
    return "Gui" not in type(message).__name__ and not is_audio_message(message)


@dataclass
class TimelineStep:
    """Recorded messages and touched node names for one timestep."""

    messages: list[_messages.Message] = field(default_factory=list)
    node_names: set[str] = field(default_factory=set)


class TimelineStore:
    """In-memory storage for timestep messages and baseline scene state."""

    def __init__(self, num_steps: int) -> None:
        self.num_steps = num_steps
        self.steps = [TimelineStep() for _ in range(num_steps)]
        self.node_names: set[str] = set()
        self.baseline_messages_by_name: dict[str, list[_messages.Message]] = {}

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

    def record_messages(
        self, step: int, messages: list[_messages.Message]
    ) -> TimelineStep:
        """Append messages to a timestep and update the touched-node index."""
        step_state = self.step(step)
        step_state.messages.extend(messages)
        for message in messages:
            if not is_scene_message(message):
                continue
            node_names = extract_node_names(message)
            step_state.node_names.update(node_names)
            self.node_names.update(node_names)
        return step_state


class TimelineRecorder(WebsockMessageHandler):
    """Temporary websocket sink used while recording a timestep."""

    def __init__(self) -> None:
        super().__init__()
        self.messages: list[_messages.Message] = []
        self._callback: Callable[[_messages.Message], None] | None = None

    def get_message_buffer(self) -> Any:
        return self

    def register_callback(self, callback: Callable[[_messages.Message], None]) -> None:
        """Register a hook that runs for every recorded message."""
        self._callback = callback

    def push(self, message: _messages.Message) -> None:
        if self._callback is not None:
            self._callback(message)
        self.messages.append(message)

    def atomic_start(self) -> None:
        return

    def atomic_end(self) -> None:
        return


def serialize_viser_recording(
    messages: list[tuple[float, SerializedMessage]],
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
