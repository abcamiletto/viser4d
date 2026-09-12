"""Helpers shared by the viser4d test modules."""

import json
from typing import Any, cast

import msgspec
import zstandard


def deserialize_recording(blob: bytes) -> dict[str, object]:
    """Decode a ``server.serialize()`` blob into viser's recording dict."""
    inner_size = int.from_bytes(blob[:8], "little")
    inner = zstandard.ZstdDecompressor().decompress(
        blob[8:], max_output_size=inner_size
    )
    assert len(inner) == inner_size
    msgpack_size = int.from_bytes(inner[:8], "little")
    return cast(dict[str, object], msgspec.msgpack.decode(inner[8 : 8 + msgpack_size]))


def exported_timeline(blob: bytes) -> dict[str, Any]:
    recording = deserialize_recording(blob)
    prefix = "window.__VISER4D__.loadRecording("
    commands = cast(list[tuple[float, dict[str, Any]]], recording["messages"])
    source = next(
        m["source"] for _, m in commands if m.get("source", "").startswith(prefix)
    )
    return json.loads(json.loads(source[len(prefix) : -2]))


def scene_events(blob: bytes) -> list[tuple[float, dict[str, Any]]]:
    """Enumerate static and recorded scene puts in the exported timeline."""
    native = deserialize_recording(blob)
    events = cast(list[tuple[float, dict[str, Any]]], native["messages"])
    timeline = exported_timeline(blob)
    events.extend((0.0, e["message"]) for e in timeline["block"]["checkpointScene"])
    for step, delta in enumerate(timeline["block"]["deltas"]):
        events.extend((step / timeline["fps"], e["message"]) for e in delta["puts"])
    return events
