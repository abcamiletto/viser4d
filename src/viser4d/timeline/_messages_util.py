"""Pure message conversion utilities for timeline storage and transport."""

from dataclasses import dataclass, field
from typing import cast

from .. import _viser_private as impl
from .._types import StoredMessage
from ..audio._messages import is_audio_message_type


@dataclass
class TimelineStep:
    """Recorded scene and audio updates for one timestep."""

    scene_updates: dict[str, StoredMessage] = field(default_factory=dict)
    audio_updates: list[StoredMessage] = field(default_factory=list)


def store_raw_message(message: impl.Message) -> StoredMessage:
    """Capture one viser message in placeholder-plus-buffer form."""
    buffers: list[memoryview] = []
    payload = cast(
        dict[str, object],
        message.as_serializable_dict(binary_buffers=buffers),
    )
    return StoredMessage(payload, tuple(bytes(buffer) for buffer in buffers))


def stored_int(value: object) -> int:
    if not isinstance(value, (int, float)):
        raise TypeError(f"Expected int-like stored value, got {type(value).__name__}.")
    return int(value)


def stored_float(value: object) -> float:
    if not isinstance(value, (int, float)):
        raise TypeError(
            f"Expected float-like stored value, got {type(value).__name__}."
        )
    return float(value)


def stored_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise TypeError(f"Expected dict stored value, got {type(value).__name__}.")
    return cast(dict[str, object], value)


def extract_message_name(message: StoredMessage) -> str | None:
    name = message.payload.get("name")
    return name if isinstance(name, str) and name else None


def is_scene_message(message: StoredMessage) -> bool:
    message_type = message.payload.get("type")
    return (
        isinstance(message_type, str)
        and not message_type.startswith("Gui")
        and not is_audio_message_type(message_type)
    )
