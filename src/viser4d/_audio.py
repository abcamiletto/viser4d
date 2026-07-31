"""Timeline-synced audio: the public handle/API and waveform normalization.

Waveforms travel as flat, frame-major float32 arrays (viser's msgpack inlines
them as binary). Integer input is normalized to ``[-1, 1]``. Handles keep the
caller's original samples so ``waveform`` round-trips losslessly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import numpy as np

from . import _viser
from ._config import require_positive_int
from ._protocol import (
    AddAudioMessage,
    AppendAudioMessage,
    RemoveAudioMessage,
    SetAudioVolumeMessage,
    SetAudioWaveformMessage,
    Waveform,
)

if TYPE_CHECKING:
    from ._recorder import Recorder


def _normalize(array: np.ndarray) -> np.ndarray:
    arr = np.asarray(array)
    if arr.ndim == 1 or (arr.ndim == 2 and arr.shape[1] in (1, 2)):
        return arr
    raise ValueError(
        "Audio data must be mono or stereo with shape (frames,) or (frames, channels)."
    )


def _to_float32(arr: np.ndarray) -> np.ndarray:
    if np.issubdtype(arr.dtype, np.signedinteger):
        info = np.iinfo(arr.dtype)
        return arr.astype(np.float32) / float(max(abs(info.min), info.max))
    if np.issubdtype(arr.dtype, np.unsignedinteger):
        midpoint = np.iinfo(arr.dtype).max / 2.0
        return (arr.astype(np.float32) - midpoint) / midpoint
    return arr.astype(np.float32, copy=False)


def make_waveform(array: np.ndarray) -> Waveform:
    """Normalize samples to a flat frame-major float32 ``Waveform`` payload."""
    arr = _normalize(array)
    channels = 1 if arr.ndim == 1 else int(arr.shape[1])
    data = np.ascontiguousarray(_to_float32(arr)).reshape(-1)
    return {"numChannels": channels, "numFrames": int(arr.shape[0]), "data": data}


class _TrackBuffer:
    """Caller-facing sample accumulator for one track (original dtype)."""

    def __init__(self, name: str, sample_rate: int, waveform: np.ndarray) -> None:
        self.name = name
        self.sample_rate = require_positive_int("sample_rate", sample_rate)
        self.volume = 1.0
        self._chunks = [_normalize(waveform)]

    @property
    def waveform(self) -> np.ndarray:
        return (
            self._chunks[0] if len(self._chunks) == 1 else np.concatenate(self._chunks)
        )

    @waveform.setter
    def waveform(self, value: np.ndarray) -> None:
        self._chunks = [_normalize(value)]

    def prepare_append(self, data: np.ndarray) -> np.ndarray:
        chunk = _normalize(data)
        if chunk.shape[1:] != self._chunks[0].shape[1:]:
            raise ValueError("Audio append must preserve channel count.")
        return chunk

    def commit_append(self, chunk: np.ndarray) -> None:
        self._chunks.append(chunk)


class AudioHandle:
    """Handle for a timeline-synced audio track."""

    def __init__(
        self, dispatch: Callable[[_viser.Message], None], buffer: _TrackBuffer
    ) -> None:
        self._dispatch = dispatch
        self._buffer = buffer

    @property
    def volume(self) -> float:
        return self._buffer.volume

    @volume.setter
    def volume(self, value: float) -> None:
        volume = float(value)
        self._dispatch(SetAudioVolumeMessage(self._buffer.name, volume))
        self._buffer.volume = volume

    @property
    def waveform(self) -> np.ndarray:
        return self._buffer.waveform.copy()

    @waveform.setter
    def waveform(self, value: np.ndarray) -> None:
        waveform = _normalize(value)
        self._dispatch(
            SetAudioWaveformMessage(self._buffer.name, make_waveform(waveform))
        )
        self._buffer.waveform = waveform

    def append(self, data: np.ndarray) -> None:
        chunk = self._buffer.prepare_append(data)
        self._dispatch(AppendAudioMessage(self._buffer.name, make_waveform(chunk)))
        self._buffer.commit_append(chunk)

    def remove(self) -> None:
        self._dispatch(RemoveAudioMessage(self._buffer.name))


class AudioApi:
    """Entry point for timeline audio creation (valid only inside ``at(t)``)."""

    def __init__(self, recorder: Recorder) -> None:
        self._recorder = recorder

    def add_track(
        self, name: str, *, data: np.ndarray, sample_rate: int
    ) -> AudioHandle:
        return self._recorder.add_audio(name, data=data, sample_rate=sample_rate)


def add_audio_message(buffer: _TrackBuffer) -> AddAudioMessage:
    return AddAudioMessage(
        name=buffer.name,
        sampleRate=buffer.sample_rate,
        waveform=make_waveform(buffer.waveform),
        volume=buffer.volume,
    )
