from __future__ import annotations

import dataclasses
import uuid

from viser import _messages
from typing_extensions import override

from ._protocol import AudioArrayPayload


class _AudioMessage(_messages.Message):
    name: str


def is_audio_message(message: object) -> bool:
    return isinstance(message, _AudioMessage)


@dataclasses.dataclass
class AddAudioMessage(_AudioMessage):
    name: str
    sampleRate: int
    waveform: AudioArrayPayload
    volume: float

    @override
    def redundancy_key(self) -> str:
        return f"create-or-remove-audio-{self.name}"


@dataclasses.dataclass
class SetAudioVolumeMessage(_AudioMessage):
    name: str
    volume: float

    @override
    def redundancy_key(self) -> str:
        return f"audio-volume-{self.name}"


@dataclasses.dataclass
class SetAudioWaveformMessage(_AudioMessage):
    name: str
    waveform: AudioArrayPayload

    @override
    def redundancy_key(self) -> str:
        return f"audio-waveform-{self.name}"


@dataclasses.dataclass
class AppendAudioMessage(_AudioMessage):
    name: str
    waveform: AudioArrayPayload

    @override
    def redundancy_key(self) -> str:
        return str(uuid.uuid4())


@dataclasses.dataclass
class RemoveAudioMessage(_AudioMessage):
    name: str

    @override
    def redundancy_key(self) -> str:
        return f"create-or-remove-audio-{self.name}"
