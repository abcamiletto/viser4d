"""Audio API and handle — syncs audio playback to the timeline.

Audio tracks are encoded as WAV, base64-encoded, and sent to the browser via
viser's RunJavascriptMessage. The browser uses HTMLAudioElement for playback.

The API mirrors viser's scene-node pattern: ``scene.add_audio()`` returns an
``AudioHandle`` whose properties (e.g. ``volume``) sync to the client, just
like ``scene.add_point_cloud()`` returns a ``PointCloudHandle``.
"""

from __future__ import annotations

import base64
import io
import wave
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from viser._messages import RunJavascriptMessage

if TYPE_CHECKING:
    from viser import ClientHandle

    from ..server import Viser4dServer

_AUDIO_RUNTIME_JS = (Path(__file__).parent / "runtime.js").read_text()


def _numpy_to_wav(data: np.ndarray, sample_rate: int) -> bytes:
    """Encode a numpy array as WAV bytes.

    Args:
        data: Audio samples. Supported dtypes: ``int16``, ``float32``.
            1-D for mono, 2-D ``(N, channels)`` for multi-channel.
        sample_rate: Sample rate in Hz.

    Returns:
        Raw WAV file bytes.

    Raises:
        TypeError: If *data* has an unsupported dtype.
    """
    if data.dtype == np.float32:
        data = np.clip(data, -1.0, 1.0)
        data = (data * 32767).astype(np.int16)
    elif data.dtype != np.int16:
        raise TypeError(f"Unsupported dtype {data.dtype}; expected int16 or float32")

    if data.ndim == 1:
        n_channels = 1
    else:
        n_channels = data.shape[1]

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(data.tobytes())
    return buf.getvalue()


class AudioHandle:
    """Handle for an audio track, returned by ``scene.add_audio()``.

    Mirrors viser's handle pattern: property assignments sync to the client.

    Example::

        handle = scene.add_audio("/bgm", data=samples, sample_rate=44100)
        handle.volume = 0.5   # immediately sent to browser
        handle.remove()       # tear down the track
    """

    def __init__(
        self,
        name: str,
        base64_wav: str,
        start_step: int,
        api: AudioApi,
    ) -> None:
        self._name = name
        self._base64_wav = base64_wav
        self._start_step = start_step
        self._volume = 1.0
        self._api = api

    @property
    def volume(self) -> float:
        """Audio volume (0.0 – 1.0). Changes are sent to the client."""
        return self._volume

    @volume.setter
    def volume(self, value: float) -> None:
        if self._volume == value:
            return
        self._volume = value
        self._api._broadcast_js(
            f"window.__viser4d_audio.setVolume({self._name!r}, {value});"
        )

    def remove(self) -> None:
        """Remove this audio track."""
        self._api.remove_track(self._name)


class AudioApi:
    """Audio API for viser4d — manages tracks and syncs playback to clients.

    Created once by ``Viser4dServer`` and wired into playback notifications
    via ``_on_playback_start`` / ``_on_playback_stop``.

    Tracks per-client state: each connecting client receives the JS runtime
    and all registered tracks. Disconnecting clients are cleaned up.
    """

    def __init__(self, server: Viser4dServer) -> None:
        self._server = server
        self._tracks: dict[str, AudioHandle] = {}
        self._initialized_clients: set[int] = set()
        self._playing = False
        self._play_fps = 30.0
        self._started_tracks: set[str] = set()

        server.on_client_connect(self._on_client_connect)
        server.on_client_disconnect(self._on_client_disconnect)
        server.on_timestep_change(self._on_timestep)

    def _on_client_connect(self, client: ClientHandle) -> None:
        """Send JS runtime + all tracks to a newly connected client."""
        if not self._tracks:
            return
        self._init_client(client)
        if self._playing:
            step = self._server._current_time
            self._send_js_to_client(
                client,
                f"window.__viser4d_audio.play({step}, {self._play_fps});",
            )

    def _on_client_disconnect(self, client: ClientHandle) -> None:
        self._initialized_clients.discard(client.client_id)

    def _on_timestep(self, step: int) -> None:
        """Check if any tracks should start at this step."""
        if not self._playing:
            return
        for name, h in self._tracks.items():
            if name not in self._started_tracks and step >= h._start_step:
                self._started_tracks.add(name)
                self._broadcast_js(
                    f"window.__viser4d_audio.startTrack({name!r});"
                )

    def add_track(
        self,
        name: str,
        data: np.ndarray,
        sample_rate: int,
        start_step: int,
    ) -> AudioHandle:
        """Encode audio data, store, and return a handle."""
        wav_bytes = _numpy_to_wav(data, sample_rate)
        b64 = base64.b64encode(wav_bytes).decode("ascii")
        handle = AudioHandle(name, b64, start_step, api=self)
        self._tracks[name] = handle
        self._broadcast_js(
            f"window.__viser4d_audio.addTrack("
            f"{name!r}, {b64!r}, {start_step}, {handle.volume});"
        )
        return handle

    def remove_track(self, name: str) -> None:
        """Remove a track by name."""
        self._tracks.pop(name, None)
        self._started_tracks.discard(name)
        self._broadcast_js(f"window.__viser4d_audio.removeTrack({name!r});")

    def on_play(self, current_step: int, fps: float) -> None:
        self._playing = True
        self._play_fps = fps
        self._started_tracks.clear()
        # Immediately start any tracks whose start_step <= current_step.
        for name, h in self._tracks.items():
            if current_step >= h._start_step:
                self._started_tracks.add(name)
        self._broadcast_js(
            f"window.__viser4d_audio.play({current_step}, {fps});"
        )

    def on_pause(self) -> None:
        self._playing = False
        self._started_tracks.clear()
        self._broadcast_js("window.__viser4d_audio.pause();")

    def on_seek(self, step: int, fps: float) -> None:
        self._broadcast_js(f"window.__viser4d_audio.seek({step}, {fps});")

    def _init_client(self, client: ClientHandle) -> None:
        """Send JS runtime + all tracks to a single client."""
        if client.client_id in self._initialized_clients:
            return
        self._initialized_clients.add(client.client_id)
        self._send_js_to_client(client, _AUDIO_RUNTIME_JS)
        for h in self._tracks.values():
            self._send_js_to_client(
                client,
                f"window.__viser4d_audio.addTrack("
                f"{h._name!r}, {h._base64_wav!r}, "
                f"{h._start_step}, {h._volume});",
            )

    def _send_js_to_client(self, client: ClientHandle, source: str) -> None:
        client._websock_connection.queue_message(
            RunJavascriptMessage(source=source)
        )

    def _broadcast_js(self, source: str) -> None:
        """Send JS to all connected clients, initializing any that need it."""
        if not self._tracks:
            return
        msg = RunJavascriptMessage(source=source)
        for client in self._server.get_clients().values():
            if client.client_id not in self._initialized_clients:
                self._init_client(client)
            client._websock_connection.queue_message(msg)
