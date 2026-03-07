from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from ._server import Viser4dServer


def _audio_samples_for_transport(array: np.ndarray) -> np.ndarray:
    arr = np.ascontiguousarray(array)
    if np.issubdtype(arr.dtype, np.signedinteger):
        info = np.iinfo(arr.dtype)
        scale = max(abs(info.min), info.max)
        return arr.astype(np.float32) / float(scale)
    if np.issubdtype(arr.dtype, np.unsignedinteger):
        info = np.iinfo(arr.dtype)
        midpoint = info.max / 2.0
        return (arr.astype(np.float32) - midpoint) / midpoint
    return arr.astype(np.float32, copy=False)


def audio_array_payload(array: np.ndarray) -> dict[str, str]:
    arr = _audio_samples_for_transport(array)
    return {
        "dtype": str(arr.dtype),
        "data": base64.b64encode(arr.tobytes()).decode("ascii"),
    }


@dataclass
class AudioState:
    name: str
    sample_rate: int
    waveform: np.ndarray
    volume: float = 1.0


class AudioHandle:
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
            self._state.name,
            {"op": "set_volume", "name": self._state.name, "volume": self._state.volume},
        )

    @property
    def waveform(self) -> np.ndarray:
        return self._state.waveform.copy()

    @waveform.setter
    def waveform(self, value: np.ndarray) -> None:
        self._state.waveform = np.ascontiguousarray(value)
        self._server._dispatch_audio_update(
            self._state.name,
            {
                "op": "set_waveform",
                "name": self._state.name,
                "waveform": audio_array_payload(self._state.waveform),
            },
        )

    def append(self, data: np.ndarray) -> None:
        append_data = np.ascontiguousarray(data)
        self._state.waveform = np.concatenate([self._state.waveform, append_data])
        self._server._dispatch_audio_update(
            self._state.name,
            {
                "op": "append",
                "name": self._state.name,
                "waveform": audio_array_payload(append_data),
            },
        )

    def remove(self) -> None:
        self._server._dispatch_audio_update(
            self._state.name,
            {"op": "remove", "name": self._state.name},
        )
