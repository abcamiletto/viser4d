from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


JSONValue = str | int | float | bool | None | list["JSONValue"] | dict[str, "JSONValue"]


@dataclass
class RecordingPayload:
    duration_seconds: float
    viser_version: str
    messages: list[tuple[float, dict[str, Any]]]


@dataclass
class TransformKeyframe:
    frame: int
    position: list[float] | None = None
    rotation_wxyz: list[float] | None = None


@dataclass
class VisibilityKeyframe:
    frame: int
    visible: bool


@dataclass
class GeometryKeyframe:
    frame: int
    payload: dict[str, JSONValue]


@dataclass
class NodeManifest:
    name: str
    kind: str
    parent_name: str | None
    create_frame: int
    create_payload: dict[str, JSONValue]
    transform_keyframes: list[TransformKeyframe]
    visibility_keyframes: list[VisibilityKeyframe]
    geometry_keyframes: list[GeometryKeyframe]


@dataclass
class RecordingManifest:
    schema_version: int
    fps: float
    frame_count: int
    source_viser_version: str
    nodes: list[NodeManifest]

    def to_jsonable(self) -> dict[str, JSONValue]:
        return asdict(self)
