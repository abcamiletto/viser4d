from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypeAlias, TypedDict

import numpy as np

StoredScalar: TypeAlias = str | int | float | bool | None
StoredPayload: TypeAlias = dict[str, object]

RuntimeArray: TypeAlias = np.ndarray[Any, np.dtype[Any]]
RuntimeValue: TypeAlias = (
    StoredScalar
    | RuntimeArray
    | list["RuntimeValue"]
    | tuple["RuntimeValue", ...]
    | dict[str, "RuntimeValue"]
)
RuntimePayload: TypeAlias = dict[str, "RuntimeValue"]


@dataclass(frozen=True)
class StoredMessage:
    payload: StoredPayload
    buffers: tuple[bytes, ...] = ()


class StoredMessageEntry(TypedDict):
    key: str
    message: StoredMessage


class StoredStatePatch(TypedDict):
    scenePuts: list[StoredMessageEntry]
    sceneDeleteNodes: list[str]
    audioMessages: list[StoredMessage]


class StepPatchUpdate(TypedDict):
    stepOffset: int
    patch: StoredStatePatch


class AudioArrayPayload(TypedDict):
    dtype: str
    numChannels: int
    numFrames: int
    data: str


class BlockManifestPayload(TypedDict):
    blockIndex: int
    stepStart: int
    stepStop: int
    payloadByteSize: int | None


class RuntimeConfig(TypedDict):
    numSteps: int
    blockSize: int
    timelineFps: float
    speed: float
    loop: bool
    chunkCacheVersion: str
    clientChunkCacheBytes: int
    blockManifests: list[BlockManifestPayload]


class ClientRuntimeConfig(RuntimeConfig):
    timelineSliderUuid: str
    speedSliderUuid: str
    stepButtonsUuid: str
    playButtonUuid: str
    pauseButtonUuid: str


class RuntimeBlockPayload(TypedDict):
    block: int
    checkpointSceneEntries: list[StoredMessageEntry]
    checkpointAudioMessages: list[StoredMessage]
    stepPatches: list[StoredStatePatch]


class RuntimeBlockPatchPayload(TypedDict):
    block: int
    checkpointScenePuts: list[StoredMessageEntry]
    checkpointSceneDeletes: list[str]
    checkpointAudioPuts: list[StoredMessage]
    checkpointAudioDeletes: list[str]
    stepPatchUpdates: list[StepPatchUpdate]
