import dataclasses
import uuid

from typing_extensions import override

from .. import _viser_private as impl
from .._types import AudioArrayPayload


class _AudioMessage(impl.Message):
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


def is_audio_message(message: object) -> bool:
    return isinstance(message, _AudioMessage)


def is_audio_message_type(message_type: object) -> bool:
    return isinstance(message_type, str) and message_type in AUDIO_MESSAGE_TYPES


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
