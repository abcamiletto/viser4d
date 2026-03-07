from __future__ import annotations

import base64
import contextlib
from dataclasses import dataclass, field
from typing import Any, Iterator

import numpy as np
from viser import _messages
from viser.infra import StateSerializer


def to_jsonable(value: Any) -> Any:
    if isinstance(value, memoryview):
        value = value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return {"__viser4d_binary__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, np.ndarray):
        return {"__viser4d_binary__": base64.b64encode(value.tobytes()).decode("ascii")}
    if isinstance(value, dict):
        return {key: to_jsonable(val) for key, val in value.items()}
    if isinstance(value, (tuple, list)):
        return [to_jsonable(item) for item in value]
    return value


def extract_node_names(message: dict[str, Any]) -> set[str]:
    name = message.get("name")
    return {name} if isinstance(name, str) and name else set()


def is_scene_message(message: Any) -> bool:
    return "Gui" not in type(message).__name__


@dataclass
class TimelineStep:
    messages: list[dict[str, Any]] = field(default_factory=list)
    node_names: set[str] = field(default_factory=set)
    audio_ops: list[dict[str, Any]] = field(default_factory=list)


class TimelineStore:
    def __init__(self, num_steps: int) -> None:
        self.num_steps = num_steps
        self.steps = [TimelineStep() for _ in range(num_steps)]
        self.node_names: set[str] = set()
        self.baseline_messages_by_name: dict[str, list[dict[str, Any]]] = {}

    def validate_step(self, step: int) -> int:
        if step < 0 or step >= self.num_steps:
            raise IndexError(
                f"Timestep {step} is out of range for {self.num_steps} steps."
            )
        return step

    def step(self, step: int) -> TimelineStep:
        return self.steps[self.validate_step(step)]

    def record_scene_messages(
        self, step: int, messages: list[dict[str, Any]]
    ) -> TimelineStep:
        step_state = self.step(step)
        step_state.messages.extend(messages)
        for message in messages:
            node_names = extract_node_names(message)
            step_state.node_names.update(node_names)
            self.node_names.update(node_names)
        return step_state

    def record_audio_ops(self, step: int, ops: list[dict[str, Any]]) -> TimelineStep:
        step_state = self.step(step)
        step_state.audio_ops.extend(ops)
        return step_state


class TimelineRecorder:
    def __init__(self) -> None:
        self.messages: list[_messages.Message] = []

    def queue_message(self, message: _messages.Message) -> None:
        self.messages.append(message)

    def get_message_buffer(self) -> Any:
        raise RuntimeError("Timeline recorder does not expose a live message buffer.")

    @contextlib.contextmanager
    def atomic(self) -> Iterator[None]:
        yield

def serialize_viser_messages(
    messages: list[_messages.Message],
    *,
    duration_seconds: float = 0.0,
) -> bytes:
    class _SerializerHandler:
        def __init__(self) -> None:
            self._record_handles: list[StateSerializer] = []

    handler = _SerializerHandler()
    serializer = StateSerializer(handler, filter=lambda _message: True)
    handler._record_handles.append(serializer)
    for message in messages:
        serializer._insert_message(message)
    if duration_seconds > 0.0:
        serializer.insert_sleep(duration_seconds)
    return serializer.serialize()
