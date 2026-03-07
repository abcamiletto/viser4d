from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import numpy as np

from ._protocol import (
    AppendAudioOp,
    AudioArrayPayload,
    RemoveAudioOp,
    SetAudioVolumeOp,
    SetAudioWaveformOp,
)

if TYPE_CHECKING:
    from ._server import Viser4dServer


def _normalize_audio_array(array: np.ndarray) -> np.ndarray:
    arr = np.asarray(array)
    if arr.ndim == 1:
        return np.ascontiguousarray(arr)
    if arr.ndim == 2 and arr.shape[1] in (1, 2):
        return np.ascontiguousarray(arr)
    raise ValueError(
        "Audio data must be mono or stereo with shape (frames,) or (frames, channels)."
    )


def _audio_layout(array: np.ndarray) -> tuple[int, int]:
    if array.ndim == 1:
        return (1, int(array.shape[0]))
    return (int(array.shape[1]), int(array.shape[0]))


def _audio_samples_for_transport(array: np.ndarray) -> np.ndarray:
    arr = _normalize_audio_array(array)
    if np.issubdtype(arr.dtype, np.signedinteger):
        info = np.iinfo(arr.dtype)
        scale = max(abs(info.min), info.max)
        return arr.astype(np.float32) / float(scale)
    if np.issubdtype(arr.dtype, np.unsignedinteger):
        info = np.iinfo(arr.dtype)
        midpoint = info.max / 2.0
        return (arr.astype(np.float32) - midpoint) / midpoint
    return arr.astype(np.float32, copy=False)


def audio_array_payload(array: np.ndarray) -> AudioArrayPayload:
    arr = _audio_samples_for_transport(array)
    num_channels, num_frames = _audio_layout(arr)
    return {
        "dtype": str(arr.dtype),
        "numChannels": num_channels,
        "numFrames": num_frames,
        "data": base64.b64encode(arr.tobytes()).decode("ascii"),
    }


class AudioState:
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
        if _audio_layout(chunk)[0] != _audio_layout(self._chunks[0])[0]:
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
            SetAudioVolumeOp(
                op="set_volume",
                name=self._state.name,
                volume=self._state.volume,
            )
        )

    @property
    def waveform(self) -> np.ndarray:
        return self._state.waveform.copy()

    @waveform.setter
    def waveform(self, value: np.ndarray) -> None:
        self._state.waveform = value
        self._server._dispatch_audio_update(
            SetAudioWaveformOp(
                op="set_waveform",
                name=self._state.name,
                waveform=audio_array_payload(self._state.waveform),
            )
        )

    def append(self, data: np.ndarray) -> None:
        append_data = self._state.append_chunk(data)
        self._server._dispatch_audio_update(
            AppendAudioOp(
                op="append",
                name=self._state.name,
                waveform=audio_array_payload(append_data),
            )
        )

    def remove(self) -> None:
        self._server._dispatch_audio_update(
            RemoveAudioOp(op="remove", name=self._state.name)
        )
