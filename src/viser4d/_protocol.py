from __future__ import annotations

from typing import Literal, TypeAlias, TypedDict


JSONValue: TypeAlias = (
    None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]
)
SerializedMessage: TypeAlias = dict[str, JSONValue]


class BinaryPayload(TypedDict):
    __viser4d_binary__: str


class AudioArrayPayload(TypedDict):
    dtype: str
    numChannels: int
    numFrames: int
    data: str


class AddAudioOp(TypedDict):
    op: Literal["add"]
    name: str
    sampleRate: int
    waveform: AudioArrayPayload
    volume: float


class SetAudioVolumeOp(TypedDict):
    op: Literal["set_volume"]
    name: str
    volume: float


class SetAudioWaveformOp(TypedDict):
    op: Literal["set_waveform"]
    name: str
    waveform: AudioArrayPayload


class AppendAudioOp(TypedDict):
    op: Literal["append"]
    name: str
    waveform: AudioArrayPayload


class RemoveAudioOp(TypedDict):
    op: Literal["remove"]
    name: str


AudioOp: TypeAlias = (
    AddAudioOp
    | SetAudioVolumeOp
    | SetAudioWaveformOp
    | AppendAudioOp
    | RemoveAudioOp
)


class RuntimeConfig(TypedDict):
    numSteps: int
    fps: float
    baseFps: float
    loop: bool


class ClientRuntimeConfig(RuntimeConfig):
    timestepSyncUuid: str


class PreloadSceneStepPayload(TypedDict):
    step: int
    messages: list[SerializedMessage]
    nodeNames: list[str]


class PreloadAudioStepPayload(TypedDict):
    step: int
    ops: list[AudioOp]


class SetBaselinePayload(TypedDict):
    name: str
    messages: list[SerializedMessage]


class PlayPayload(TypedDict):
    fps: float
    loop: bool


class SeekPayload(TypedDict):
    step: int


class EmptyPayload(TypedDict):
    pass


RuntimeMethod: TypeAlias = Literal[
    "applyAudioUpdate",
    "configure",
    "pause",
    "play",
    "preloadAudioStep",
    "preloadSceneStep",
    "seek",
    "setBaseline",
    "setFps",
]

RuntimePayload: TypeAlias = (
    AudioOp
    | ClientRuntimeConfig
    | EmptyPayload
    | PlayPayload
    | PreloadAudioStepPayload
    | PreloadSceneStepPayload
    | RuntimeConfig
    | SeekPayload
    | SetBaselinePayload
)
