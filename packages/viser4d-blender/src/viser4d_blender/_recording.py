from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import msgspec
import zstandard

from ._types import RecordingPayload


def load_viser_recording(path: str | Path) -> RecordingPayload:
    input_path = Path(path)
    raw = input_path.read_bytes()
    if len(raw) < 8:
        raise ValueError(f"{input_path} is too short to be a valid .viser file.")

    packed_size = int.from_bytes(raw[:8], "little")
    packed = zstandard.ZstdDecompressor().decompress(raw[8:])
    if packed_size != len(packed):
        raise ValueError(
            f"{input_path} decompressed to {len(packed)} bytes, expected {packed_size}."
        )

    payload = cast(dict[str, Any], msgspec.msgpack.decode(packed))
    duration_seconds = payload.get("durationSeconds")
    viser_version = payload.get("viserVersion")
    messages = payload.get("messages")
    if not isinstance(duration_seconds, (int, float)):
        raise ValueError(f"{input_path} is missing durationSeconds.")
    if not isinstance(viser_version, str):
        raise ValueError(f"{input_path} is missing viserVersion.")
    if not isinstance(messages, list):
        raise ValueError(f"{input_path} is missing messages.")

    parsed_messages: list[tuple[float, dict[str, Any]]] = []
    for entry in messages:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise ValueError(f"{input_path} has an invalid message entry: {entry!r}")
        time_value, message = entry
        if not isinstance(time_value, (int, float)) or not isinstance(message, dict):
            raise ValueError(f"{input_path} has an invalid message entry: {entry!r}")
        parsed_messages.append((float(time_value), cast(dict[str, Any], message)))

    return RecordingPayload(
        duration_seconds=float(duration_seconds),
        viser_version=viser_version,
        messages=parsed_messages,
    )
