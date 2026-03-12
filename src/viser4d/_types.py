from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, TypeAlias, TypedDict


JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)
SerializedMessage: TypeAlias = dict[str, JSONValue]
StoredValue: TypeAlias = (
    None
    | bool
    | int
    | float
    | str
    | bytes
    | list["StoredValue"]
    | dict[str, "StoredValue"]
)
StoredMessage: TypeAlias = dict[str, StoredValue]


class BinaryPayload(TypedDict):
    __viser4d_binary__: str


class AudioArrayPayload(TypedDict):
    dtype: str
    numChannels: int
    numFrames: int
    data: str


class RuntimeConfig(TypedDict):
    numSteps: int
    fps: float
    baseFps: float
    loop: bool


class ClientRuntimeConfig(RuntimeConfig):
    timestepSyncUuid: str


RuntimeMethod: TypeAlias = Literal[
    "applyMessageUpdate",
    "configure",
    "pause",
    "play",
    "preloadStep",
    "seek",
    "setBaseline",
    "setFps",
]

RuntimePayload: TypeAlias = Mapping[str, object]
