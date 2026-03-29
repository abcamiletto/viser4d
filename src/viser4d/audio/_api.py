from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import numpy as np

from ._messages import (
    AppendAudioMessage,
    RemoveAudioMessage,
    SetAudioVolumeMessage,
    SetAudioWaveformMessage,
)
from .._types import (
    AudioArrayPayload,
)

if TYPE_CHECKING:
    from .._server import Viser4dServer


def _normalize_audio_array(array: np.ndarray) -> np.ndarray:
    arr = np.asarray(array)
    if arr.ndim == 1:
        return np.ascontiguousarray(arr)
    if arr.ndim == 2 and arr.shape[1] in (1, 2):
        return np.ascontiguousarray(arr)
    raise ValueError(
        "Audio data must be mono or stereo with shape (frames,) or (frames, channels)."
    )


def _to_float32(array: np.ndarray) -> np.ndarray:
    """Normalize and convert audio samples to float32 for transport."""
    arr = _normalize_audio_array(array)
    if np.issubdtype(arr.dtype, np.signedinteger):
        info = np.iinfo(arr.dtype)
        return arr.astype(np.float32) / float(max(abs(info.min), info.max))
    if np.issubdtype(arr.dtype, np.unsignedinteger):
        midpoint = np.iinfo(arr.dtype).max / 2.0
        return (arr.astype(np.float32) - midpoint) / midpoint
    return arr.astype(np.float32, copy=False)


def audio_array_payload(array: np.ndarray) -> AudioArrayPayload:
    arr = _to_float32(array)
    num_channels = 1 if arr.ndim == 1 else int(arr.shape[1])
    num_frames = int(arr.shape[0])
    return {
        "dtype": str(arr.dtype),
        "numChannels": num_channels,
        "numFrames": num_frames,
        "data": base64.b64encode(arr.tobytes()).decode("ascii"),
    }


class AudioState:
    """Mutable waveform state backing one timeline audio track."""

    def __init__(
        self,
        *,
        name: str,
        sample_rate: int,
        waveform: np.ndarray,
        volume: float = 1.0,
    ) -> None:
        self.name = name
        self.sample_rate = sample_rate
        self.volume = volume
        self._chunks: list[np.ndarray] = []
        self._waveform_cache: np.ndarray | None = None
        self.waveform = waveform

    @property
    def waveform(self) -> np.ndarray:
        if self._waveform_cache is None:
            if len(self._chunks) == 1:
                self._waveform_cache = self._chunks[0]
            else:
                self._waveform_cache = np.concatenate(self._chunks)
        return self._waveform_cache

    @waveform.setter
    def waveform(self, value: np.ndarray) -> None:
        arr = _normalize_audio_array(value)
        self._chunks = [arr]
        self._waveform_cache = arr

    def append_chunk(self, data: np.ndarray) -> np.ndarray:
        chunk = _normalize_audio_array(data)
        if chunk.shape[1:] != self._chunks[0].shape[1:]:
            raise ValueError("Audio append must preserve channel count.")
        self._chunks.append(chunk)
        self._waveform_cache = None
        return chunk


class AudioHandle:
    """Handle for a timeline-synced audio track."""

    def __init__(self, server: Viser4dServer, state: AudioState):
        self._server = server
        self._state = state

    @property
    def volume(self) -> float:
        return self._state.volume

    @volume.setter
    def volume(self, value: float) -> None:
        self._state.volume = float(value)
        self._server._dispatch_audio_update(
            SetAudioVolumeMessage(name=self._state.name, volume=self._state.volume)
        )

    @property
    def waveform(self) -> np.ndarray:
        return self._state.waveform.copy()

    @waveform.setter
    def waveform(self, value: np.ndarray) -> None:
        self._state.waveform = value
        self._server._dispatch_audio_update(
            SetAudioWaveformMessage(
                name=self._state.name,
                waveform=audio_array_payload(self._state.waveform),
            )
        )

    def append(self, data: np.ndarray) -> None:
        """Append samples and broadcast the incremental chunk update."""
        append_data = self._state.append_chunk(data)
        self._server._dispatch_audio_update(
            AppendAudioMessage(
                name=self._state.name,
                waveform=audio_array_payload(append_data),
            )
        )

    def remove(self) -> None:
        self._server._dispatch_audio_update(RemoveAudioMessage(name=self._state.name))


class AudioApi:
    """Entry point for timeline-aware audio creation."""

    def __init__(self, server: Viser4dServer) -> None:
        self._server = server

    def add_track(
        self,
        name: str,
        *,
        data: np.ndarray,
        sample_rate: int,
    ) -> AudioHandle:
        """Create an audio track for the current ``server.at(t)`` block."""
        if self._server._recorder.active_step is None:
            raise RuntimeError("audio.add_track() is only valid inside server.at(t).")
        return self._server._recorder.add_audio(
            name, data=data, sample_rate=sample_rate
        )
