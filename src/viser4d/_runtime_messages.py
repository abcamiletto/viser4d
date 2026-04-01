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


@dataclasses.dataclass
class Viser4dRuntimeMessage(impl.Message):
    method: RuntimeMethod
    payload: object | None

    @override
    def redundancy_key(self) -> str:
        return str(uuid.uuid4())


@dataclasses.dataclass
class Viser4dRuntimeReadyMessage(impl.Message):
    @override
    def redundancy_key(self) -> str:
        return type(self).__name__
