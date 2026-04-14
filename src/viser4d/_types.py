from dataclasses import dataclass
from typing import TypeAlias, TypedDict


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
    timelineSliderUuid: str
    speedSliderUuid: str
    stepButtonsUuid: str
    playButtonUuid: str
    pauseButtonUuid: str


class RuntimeBlockPayload(TypedDict):
    block: int
    checkpointMessages: list[StoredMessage]
    stepMessages: list[list[StoredMessage]]
