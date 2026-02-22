"""Audio API and handle — syncs audio playback to the timeline.

This module defines a strict transport contract between Python and the browser
runtime. Each transport message is a JSON payload with:

- ``seq``: monotonic sequence number (drops stale updates).
- ``step``: authoritative timeline step.
- ``timeline_fps``: fixed conversion from step to media seconds.
- ``playback_fps``: current transport speed in steps / second.
- ``playing``: whether playback is active.
- ``hard_sync``: true only for discontinuities (seek/play/pause/fps changes).

The JavaScript runtime uses this payload to run a phase-locked sync loop that
avoids frequent source restarts and only hard-resyncs when error exceeds the
configured bound.
"""

from __future__ import annotations

import base64
import io
import threading
import wave
from typing import TYPE_CHECKING

import numpy as np

from viser._messages import RunJavascriptMessage

from . import js

if TYPE_CHECKING:
    from viser import ClientHandle
    from viser.infra import StateSerializer

    from ..server import Viser4dServer


def _sanitize_fps(value: float, *, default: float) -> float:
    return value if value > 0 else default


def _to_int16_samples(data: np.ndarray) -> np.ndarray:
    if data.ndim == 1:
        data = data[:, np.newaxis]
    elif data.ndim != 2:
        raise ValueError("Audio data must be 1-D or 2-D.")
    if data.shape[1] < 1:
        raise ValueError("Audio data must have at least one channel.")

    if data.dtype == np.float32:
        data = np.clip(data, -1.0, 1.0)
        return (data * 32767).astype(np.int16)
    if data.dtype == np.int16:
        return data
    raise TypeError(f"Unsupported dtype {data.dtype}; expected int16 or float32")


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
    samples = _to_int16_samples(data)
    n_channels = samples.shape[1]

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(n_channels)
        wf.setsampwidth(2)  # 16-bit
        wf.setframerate(sample_rate)
        wf.writeframes(samples.tobytes())
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
        start_step: int,
        sample_rate: int,
        samples: np.ndarray,
        api: AudioApi,
    ) -> None:
        self._name = name
        self._start_step = start_step
        self._sample_rate = sample_rate
        self._samples = samples
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
        self._api._broadcast_js(js.call("setVolume", self._name, value))

    @property
    def waveform(self) -> np.ndarray:
        """Audio waveform samples as int16 array with shape (N, channels)."""
        return self._samples.copy()

    @waveform.setter
    def waveform(self, value: np.ndarray) -> None:
        self._samples = _to_int16_samples(value)
        self._api._push_track_update(self, hard_sync=False)

    def append(self, data: np.ndarray) -> None:
        """Append samples contiguously to this track."""
        self._api.append_to_track(self._name, data)

    def remove(self) -> None:
        """Remove this audio track."""
        self._api.remove_track(self._name)


class AudioApi:
    """Audio API for viser4d — manages tracks and syncs playback to clients.

    Created once by ``Viser4dServer`` and wired into playback notifications.

    Tracks per-client state: each connecting client receives the JS runtime
    and all registered tracks. Disconnecting clients are cleaned up.
    """

    def __init__(self, server: Viser4dServer, timeline_fps: float) -> None:
        self._server = server
        self._tracks: dict[str, AudioHandle] = {}
        self._initialized_clients: set[int] = set()
        self._state_lock = threading.Lock()
        self._transport_seq = 0
        self._playing = False
        self._timeline_fps = _sanitize_fps(timeline_fps, default=30.0)
        self._playback_fps = self._timeline_fps

        server.on_client_connect(self._on_client_connect)
        server.on_client_disconnect(self._on_client_disconnect)

    def _on_client_connect(self, client: ClientHandle) -> None:
        """Send JS runtime + all tracks to a newly connected client."""
        if not self._tracks:
            return
        self._sync_client_tracks(client)
        self._sync_client_playback_state(client)

    def _on_client_disconnect(self, client: ClientHandle) -> None:
        self._initialized_clients.discard(client.client_id)

    def _on_timestep(self, step: int) -> None:
        """Broadcast transport ticks while playback is active."""
        with self._state_lock:
            if not self._playing or not self._tracks:
                return
        self._broadcast_transport_to_initialized(step)

    def add_track(
        self,
        name: str,
        data: np.ndarray,
        sample_rate: int,
        start_step: int,
    ) -> AudioHandle:
        """Encode audio data, store, and return a handle.

        If a track with the same name already exists, incoming audio is
        concatenated to the end of the existing track.
        """
        incoming = _to_int16_samples(data)
        existing = self._tracks.get(name)

        is_update = existing is not None
        if existing is None:
            handle = AudioHandle(
                name,
                start_step,
                sample_rate,
                incoming,
                api=self,
            )
            self._tracks[name] = handle
        else:
            handle = existing
            if handle._sample_rate != sample_rate:
                raise ValueError(
                    f"Audio track {name!r} already exists with sample_rate="
                    f"{handle._sample_rate}; got {sample_rate}."
                )
            self._append_samples(handle, incoming)

        self._push_track_update(handle, hard_sync=not is_update)
        return handle

    def append_to_track(self, name: str, data: np.ndarray) -> None:
        """Append samples to an existing track by name."""
        handle = self._tracks.get(name)
        if handle is None:
            raise ValueError(f"Audio track {name!r} does not exist.")
        incoming = _to_int16_samples(data)
        self._append_samples(handle, incoming)
        self._push_track_update(handle, hard_sync=False)

    def _append_samples(self, handle: AudioHandle, incoming: np.ndarray) -> None:
        if handle._samples.shape[1] != incoming.shape[1]:
            raise ValueError(
                f"Audio track {handle._name!r} already exists with channels="
                f"{handle._samples.shape[1]}; got {incoming.shape[1]}."
            )
        handle._samples = np.concatenate((handle._samples, incoming), axis=0)

    def _push_track_update(self, handle: AudioHandle, *, hard_sync: bool) -> None:
        for client in self._server.get_clients().values():
            if client.client_id in self._initialized_clients:
                self._send_track_to_client(client, handle)
                self._send_transport_to_client(client, hard_sync=hard_sync)
            else:
                # Client connected before audio was initialized: send full state once.
                self._sync_client_tracks(client)
                self._sync_client_playback_state(client)

    def remove_track(self, name: str) -> None:
        """Remove a track by name."""
        removed = self._tracks.pop(name, None)
        if removed is None:
            return
        self._broadcast_js_to_initialized(js.call("removeTrack", name))

    def on_play(self, current_step: int, fps: float) -> None:
        with self._state_lock:
            self._playing = True
            self._playback_fps = _sanitize_fps(fps, default=self._timeline_fps)
        self._broadcast_transport(current_step, hard_sync=True)

    def on_pause(self, step: int) -> None:
        with self._state_lock:
            self._playing = False
        self._broadcast_transport(step, hard_sync=True)

    def on_seek(self, step: int, fps: float) -> None:
        with self._state_lock:
            self._playing = False
            self._playback_fps = _sanitize_fps(fps, default=self._timeline_fps)
        self._broadcast_transport(step, hard_sync=True)

    def on_fps_change(self, fps: float, step: int) -> None:
        with self._state_lock:
            self._playback_fps = _sanitize_fps(fps, default=self._timeline_fps)
            playing = self._playing
        if playing:
            self._broadcast_transport(step, hard_sync=True)

    def _ensure_client_runtime(self, client: ClientHandle) -> None:
        """Ensure a client has received the audio runtime JS."""
        if client.client_id in self._initialized_clients:
            return
        self._initialized_clients.add(client.client_id)
        self._send_js_to_client(client, js.RUNTIME)

    def _send_track_to_client(self, client: ClientHandle, track: AudioHandle) -> None:
        self._send_js_to_client(client, self._track_add_js(track))

    def _sync_client_tracks(self, client: ClientHandle) -> None:
        """Send runtime + current track state to a single client."""
        self._ensure_client_runtime(client)
        for h in self._tracks.values():
            self._send_track_to_client(client, h)

    def _sync_client_playback_state(self, client: ClientHandle) -> None:
        self._send_transport_to_client(client, hard_sync=True)

    def serialize_into(
        self,
        serializer: StateSerializer,
    ) -> bool:
        """Insert audio runtime/track/transport messages into a .viser serializer."""
        if not self._tracks:
            return False

        serializer._insert_message(RunJavascriptMessage(source=js.RUNTIME))
        for track in self._tracks.values():
            serializer._insert_message(
                RunJavascriptMessage(source=self._track_add_js(track))
            )
        return True

    def serialize_transport_into(
        self,
        serializer: StateSerializer,
        *,
        seq: int,
        step: int,
        fps: float,
        hard_sync: bool,
    ) -> None:
        """Insert one serialized transport update."""
        serializer._insert_message(
            RunJavascriptMessage(
                source=self._transport_source(
                    seq=int(seq),
                    step=int(step),
                    playback_fps=_sanitize_fps(fps, default=self._timeline_fps),
                    playing=True,
                    hard_sync=hard_sync,
                )
            )
        )

    def _transport_js(self, step: int, *, hard_sync: bool) -> str:
        with self._state_lock:
            self._transport_seq += 1
            return self._transport_source(
                seq=self._transport_seq,
                step=int(step),
                playback_fps=_sanitize_fps(
                    self._playback_fps, default=self._timeline_fps
                ),
                playing=self._playing,
                hard_sync=hard_sync,
            )

    def _transport_source(
        self,
        *,
        seq: int,
        step: int,
        playback_fps: float,
        playing: bool,
        hard_sync: bool,
    ) -> str:
        payload = {
            "seq": seq,
            "step": step,
            "timeline_fps": self._timeline_fps,
            "playback_fps": playback_fps,
            "playing": playing,
            "hard_sync": hard_sync,
        }
        return js.call("setTransport", payload)

    @staticmethod
    def _track_add_js(track: AudioHandle) -> str:
        base64_wav = base64.b64encode(
            _numpy_to_wav(track._samples, track._sample_rate)
        ).decode("ascii")
        return js.call(
            "addTrack", track._name, base64_wav, track._start_step, track._volume
        )

    def _send_transport_to_client(
        self,
        client: ClientHandle,
        *,
        hard_sync: bool,
    ) -> None:
        if not self._tracks:
            return
        step = self._server.current_time
        self._send_js_to_client(client, self._transport_js(step, hard_sync=hard_sync))

    def _broadcast_transport(self, step: int, *, hard_sync: bool) -> None:
        if not self._tracks:
            return
        self._broadcast_js(self._transport_js(step, hard_sync=hard_sync))

    def _broadcast_transport_to_initialized(self, step: int) -> None:
        if not self._tracks:
            return
        self._broadcast_js_to_initialized(self._transport_js(step, hard_sync=False))

    def _send_js_to_client(self, client: ClientHandle, source: str) -> None:
        client._websock_connection.queue_message(RunJavascriptMessage(source=source))

    def _broadcast_js(self, source: str) -> None:
        """Send JS to all connected clients, initializing runtime if needed."""
        msg = RunJavascriptMessage(source=source)
        for client in self._server.get_clients().values():
            self._ensure_client_runtime(client)
            client._websock_connection.queue_message(msg)

    def _broadcast_js_to_initialized(self, source: str) -> None:
        """Send JS only to already-initialized clients."""
        msg = RunJavascriptMessage(source=source)
        for client in self._server.get_clients().values():
            if client.client_id in self._initialized_clients:
                client._websock_connection.queue_message(msg)
