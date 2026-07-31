"""Wire protocol between the server and the injected browser runtime.

This module is the single source of truth for the protocol: `_codegen.py`
generates `client/protocol.gen.ts` from these definitions. Control messages
travel server -> client over viser's websocket and are intercepted by the
runtime before viser processes them; event messages travel client -> server
through viser's normal message path.

Scene message payloads (`ScenePayload`) are plain viser messages in dict form.
Numpy arrays inside payloads arrive in the browser as raw bytes, so any array
that needs interpretation carries explicit metadata (see `Waveform`).
"""

from __future__ import annotations

import dataclasses
import uuid
from typing import Any, NewType, TypeAlias, TypedDict

import numpy as np
import numpy.typing as npt
from typing_extensions import override

from . import _viser

Payload: TypeAlias = dict[str, Any]
ScenePayload = NewType("ScenePayload", Payload)
"""A viser scene message as plain data, possibly containing numpy arrays."""


class SceneEntry(TypedDict):
    """One keyed scene put. Entries with equal ``rev`` are identical."""

    key: str
    rev: int
    name: str | None
    message: ScenePayload


class AudioEvent(TypedDict):
    rev: int
    message: ScenePayload


class StepDelta(TypedDict):
    """Changes one timestep applies on top of the previous timestep's state."""

    puts: list[SceneEntry]
    deleteNodes: list[str]
    audio: list[AudioEvent]


class Waveform(TypedDict):
    """Flat float32 samples, frame-major: ``data[frame * numChannels + ch]``."""

    numChannels: int
    numFrames: int
    data: npt.NDArray[np.float32]


class AudioTrack(TypedDict):
    """Folded audio track state at a block boundary."""

    name: str
    rev: int
    sampleRate: int
    startStep: int
    volume: float
    waveform: Waveform


class BlockManifest(TypedDict):
    index: int
    stepStart: int
    stepStop: int
    byteSize: int | None


class _TimelineMessage(_viser.Message, include_in_scene_serialization=False):
    """Base for all viser4d wire messages. Never deduplicated in queues."""

    @override
    def redundancy_key(self) -> str:
        return str(uuid.uuid4())


class TimelineControlMessage(_TimelineMessage, tag="TimelineControlMessage"):  # type: ignore[invalid-argument-type]  # custom tag beyond viser's TagLiteral
    """Server -> browser runtime."""


class TimelineEventMessage(_TimelineMessage, tag="TimelineEventMessage"):  # type: ignore[invalid-argument-type]
    """Browser runtime -> server."""


# ---------------------------------------------------------------------------
# Server -> client
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TimelineConfigureMessage(TimelineControlMessage):
    numSteps: int
    blockSize: int
    timelineFps: float
    speed: float
    loop: bool
    cacheBytes: int
    manifests: list[BlockManifest]


@dataclasses.dataclass
class TimelineManifestsMessage(TimelineControlMessage):
    manifests: list[BlockManifest]


@dataclasses.dataclass
class TimelineBlockMessage(TimelineControlMessage):
    """Full payload for one block; replaces any previously held copy."""

    index: int
    checkpointScene: list[SceneEntry]
    checkpointAudio: list[AudioTrack]
    deltas: list[StepDelta]


@dataclasses.dataclass
class TimelineOverrideMessage(TimelineControlMessage):
    """One keyed entry of the live override overlay."""

    key: str
    rev: int
    name: str | None
    message: ScenePayload


@dataclasses.dataclass
class TimelineSeekMessage(TimelineControlMessage):
    step: int


@dataclasses.dataclass
class TimelinePlayMessage(TimelineControlMessage):
    speed: float
    loop: bool


@dataclasses.dataclass
class TimelinePauseMessage(TimelineControlMessage):
    pass


@dataclasses.dataclass
class TimelineSetSpeedMessage(TimelineControlMessage):
    speed: float
    loop: bool


@dataclasses.dataclass
class TimelineClearMessage(TimelineControlMessage):
    """Reset all client timeline state (blocks, overrides, transport)."""


@dataclasses.dataclass
class TimelineRefreshMessage(TimelineControlMessage):
    """Re-apply the current timestep from scratch."""


# ---------------------------------------------------------------------------
# Client -> server
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TimelineReadyMessage(TimelineEventMessage):
    pass


@dataclasses.dataclass
class TimelineBlockRequestMessage(TimelineEventMessage):
    index: int


@dataclasses.dataclass
class TimelineBlockDiscardMessage(TimelineEventMessage):
    index: int


@dataclasses.dataclass
class TimelineTimestepMessage(TimelineEventMessage):
    step: int


@dataclasses.dataclass
class TimelinePlaybackStateMessage(TimelineEventMessage):
    isPlaying: bool


@dataclasses.dataclass
class TimelineSpeedMessage(TimelineEventMessage):
    speed: float


EVENT_MESSAGE_TYPES: tuple[type[TimelineEventMessage], ...] = (
    TimelineReadyMessage,
    TimelineBlockRequestMessage,
    TimelineBlockDiscardMessage,
    TimelineTimestepMessage,
    TimelinePlaybackStateMessage,
    TimelineSpeedMessage,
)


# ---------------------------------------------------------------------------
# Recorded audio messages. These are captured into timeline storage (and
# replayed inside block payloads / audio events), never sent standalone.
# ---------------------------------------------------------------------------


class AudioMessage(_TimelineMessage, tag="AudioMessage"):  # type: ignore[invalid-argument-type]
    name: str


@dataclasses.dataclass
class AddAudioMessage(AudioMessage):
    name: str
    sampleRate: int
    waveform: Waveform
    volume: float


@dataclasses.dataclass
class SetAudioVolumeMessage(AudioMessage):
    name: str
    volume: float


@dataclasses.dataclass
class SetAudioWaveformMessage(AudioMessage):
    name: str
    waveform: Waveform


@dataclasses.dataclass
class AppendAudioMessage(AudioMessage):
    name: str
    waveform: Waveform


@dataclasses.dataclass
class RemoveAudioMessage(AudioMessage):
    name: str


AUDIO_MESSAGE_TYPES = frozenset(
    {
        "AddAudioMessage",
        "SetAudioVolumeMessage",
        "SetAudioWaveformMessage",
        "AppendAudioMessage",
        "RemoveAudioMessage",
    }
)
