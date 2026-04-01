from __future__ import annotations

import dataclasses
import uuid
from typing import Literal

from typing_extensions import override

from . import _viser_private as impl

RuntimeMethod = Literal[
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

RuntimeEvent = Literal["blockRequest", "playbackState", "ready", "speed", "timestep"]


@dataclasses.dataclass
class Viser4dRuntimeMessage(impl.Message):
    method: RuntimeMethod
    payload: object | None

    @override
    def redundancy_key(self) -> str:
        return str(uuid.uuid4())


@dataclasses.dataclass
class Viser4dRuntimeEventMessage(impl.Message):
    event: RuntimeEvent
    step: int | None = None
    speed: float | None = None
    isPlaying: bool | None = None

    @override
    def redundancy_key(self) -> str:
        return str(uuid.uuid4())
