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
    timelineSliderUuid: str
    speedSliderUuid: str
    stepButtonsUuid: str
    playButtonUuid: str
    pauseButtonUuid: str


class RuntimeBlockPayload(TypedDict):
    block: int
    checkpointMessages: list[StoredMessage]
    stepMessages: list[list[StoredMessage]]


class RuntimeBlockStepDeltaPayload(TypedDict):
    offset: int
    messages: list[StoredMessage]


class RuntimeBlockDeltaPayload(TypedDict):
    block: int
    checkpointMessages: list[StoredMessage] | None
    stepDeltas: list[RuntimeBlockStepDeltaPayload]
