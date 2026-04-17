from __future__ import annotations

import dataclasses
import uuid
from typing import ClassVar, NewType

from typing_extensions import override

from . import _viser_private as impl
from ._types import RuntimePayload

RuntimeSceneMessage = NewType("RuntimeSceneMessage", RuntimePayload)


def runtime_scene_message(message: RuntimePayload) -> RuntimeSceneMessage:
    return RuntimeSceneMessage(message)


def runtime_scene_messages(
    messages: list[RuntimePayload],
) -> list[RuntimeSceneMessage]:
    return [runtime_scene_message(message) for message in messages]


class _RuntimeMessageBase(impl.Message):
    _tags: ClassVar[tuple[str, ...]] = tuple()

    @override
    def redundancy_key(self) -> str:
        return str(uuid.uuid4())

    def __init_subclass__(cls, tag: str | None = None) -> None:
        super().__init_subclass__()
        if tag is not None:
            cls._tags = cls._tags + (tag,)


class _RuntimeControlMessage(_RuntimeMessageBase, tag="RuntimeControlMessage"):
    pass


class RuntimeEventMessage(_RuntimeMessageBase, tag="RuntimeEventMessage"):
    pass


@dataclasses.dataclass
class RuntimeClearMessage(_RuntimeControlMessage):
    pass


@dataclasses.dataclass
class RuntimeConfigureMessage(_RuntimeControlMessage):
    numSteps: int
    blockSize: int
    timelineFps: float
    speed: float
    loop: bool
    chunkCacheVersion: str
    timelineSliderUuid: str
    speedSliderUuid: str
    stepButtonsUuid: str
    playButtonUuid: str
    pauseButtonUuid: str


@dataclasses.dataclass
class RuntimeLoadBlockMessage(_RuntimeControlMessage):
    block: int
    checkpointMessages: list[RuntimeSceneMessage]
    stepMessages: list[list[RuntimeSceneMessage]]


@dataclasses.dataclass
class RuntimeEvictBlockMessage(_RuntimeControlMessage):
    block: int


@dataclasses.dataclass
class RuntimeSeekMessage(_RuntimeControlMessage):
    step: int


@dataclasses.dataclass
class RuntimeRefreshMessage(_RuntimeControlMessage):
    pass


@dataclasses.dataclass
class RuntimePlayMessage(_RuntimeControlMessage):
    speed: float
    loop: bool


@dataclasses.dataclass
class RuntimePauseMessage(_RuntimeControlMessage):
    pass


@dataclasses.dataclass
class RuntimeSetSpeedMessage(_RuntimeControlMessage):
    speed: float
    loop: bool


@dataclasses.dataclass
class RuntimeApplyMessageUpdateMessage(_RuntimeControlMessage):
    message: RuntimeSceneMessage


@dataclasses.dataclass
class RuntimeBlockRequestMessage(RuntimeEventMessage):
    step: int


@dataclasses.dataclass
class RuntimeTimestepMessage(RuntimeEventMessage):
    step: int


@dataclasses.dataclass
class RuntimeSpeedMessage(RuntimeEventMessage):
    speed: float


@dataclasses.dataclass
class RuntimePlaybackStateMessage(RuntimeEventMessage):
    isPlaying: bool


@dataclasses.dataclass
class RuntimeReadyMessage(RuntimeEventMessage):
    pass


RUNTIME_EVENT_MESSAGE_TYPES = (
    RuntimeBlockRequestMessage,
    RuntimeTimestepMessage,
    RuntimeSpeedMessage,
    RuntimePlaybackStateMessage,
    RuntimeReadyMessage,
)
