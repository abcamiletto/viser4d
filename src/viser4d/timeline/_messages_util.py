"""Pure message conversion utilities for timeline storage and transport."""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, cast

import msgspec
import numpy as np
import viser
import zstandard

from .. import _viser_private as impl
from .._types import (
    BinaryPayload,
    JSONValue,
    SerializedMessage,
    StoredMessage,
    StoredValue,
)
from ..audio._messages import is_audio_message_type


@dataclass
class TimelineStep:
    """Recorded scene and audio updates for one timestep."""

    scene_updates: dict[str, StoredMessage] = field(default_factory=dict)
    audio_updates: list[StoredMessage] = field(default_factory=list)


def to_stored(value: Any) -> StoredValue:
    """Convert a viser's serializable payload into the timeline storage form."""
    if isinstance(value, memoryview):
        return value.tobytes()
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, np.ndarray):
        return value.tobytes()
    if isinstance(value, (tuple, list)):
        return [to_stored(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_stored(val) for key, val in value.items()}
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
    if isinstance(value, (tuple, list)):
        return [to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): to_jsonable(val) for key, val in value.items()}
    return cast(JSONValue, value)


def store_raw_message(message: impl.Message) -> StoredMessage:
    """Capture one viser message in the timeline's canonical storage form."""
    return cast(StoredMessage, to_stored(message.as_serializable_dict()))


def store_raw_messages(messages: list[impl.Message]) -> list[StoredMessage]:
    return [store_raw_message(message) for message in messages]


def serialize_stored_message(message: StoredMessage) -> SerializedMessage:
    return cast(SerializedMessage, to_jsonable(message))


def serialize_stored_messages(
    messages: list[StoredMessage],
) -> list[SerializedMessage]:
    return [serialize_stored_message(message) for message in messages]


def stored_int(value: StoredValue) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"Expected int-like stored value, got {type(value).__name__}.")
    return int(value)


def stored_float(value: StoredValue) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(
            f"Expected float-like stored value, got {type(value).__name__}."
        )
    return float(value)


def stored_dict(value: StoredValue) -> dict[str, StoredValue]:
    if not isinstance(value, dict):
        raise TypeError(f"Expected dict stored value, got {type(value).__name__}.")
    return cast(dict[str, StoredValue], value)


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
