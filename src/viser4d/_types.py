from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias, TypedDict


StoredPayload: TypeAlias = dict[str, object]


@dataclass(frozen=True)
class StoredMessage:
    payload: StoredPayload
    buffers: tuple[bytes, ...] = ()


class AudioArrayPayload(TypedDict):
    dtype: str
    numChannels: int
    numFrames: int
    data: str


class RuntimeConfig(TypedDict):
    numSteps: int
    blockSize: int
    timelineFps: float
    speed: float
    loop: bool


class ClientRuntimeConfig(RuntimeConfig):
    blockRequestSyncUuid: str
    timelineSliderUuid: str
    speedSliderUuid: str
    stepButtonsUuid: str
    playButtonUuid: str
    pauseButtonUuid: str
    speedSyncUuid: str
    playbackStateSyncUuid: str
    timestepSyncUuid: str


RuntimeMethod: TypeAlias = Literal[
    "applyMessageUpdate",
    "configure",
    "evictBlock",
    "loadBlock",
    "pause",
    "play",
    "refresh",
    "seek",
    "setSpeed",
]

RuntimePayload: TypeAlias = Mapping[str, object] | StoredMessage
