"""Audio support for viser4d.

This module keeps audio timeline state on the Python side and dispatches
per-client playback commands through viser's client-local websocket channel.
"""

from __future__ import annotations

import base64
import json
import struct
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np
from viser import _messages as _viser_messages

if TYPE_CHECKING:
    from viser import ClientHandle

    from .server import Viser4dServer


@dataclass(frozen=True)
class AudioTrack:
    """Audio track data stored in the timeline."""

    track_id: str
    wav_base64: str
    start_step: int
    sample_rate: int
    num_frames: int
    num_channels: int


_AUDIO_RUNTIME_JS = """\
(function () {
  if (window.__viser4d_audio && window.__viser4d_audio.dispatch) return;

  var state = {
    tracks: {},
    playing: false,
    step: 0,
    fps: 30,
    timestampSec: 0,
    needsResume: true,
  };

  function decodeBase64ToBytes(base64Str) {
    var binary = atob(base64Str);
    var bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) {
      bytes[i] = binary.charCodeAt(i);
    }
    return bytes;
  }

  function releaseTrack(track) {
    try {
      track.audio.pause();
      URL.revokeObjectURL(track.url);
    } catch (err) {}
  }

  function currentStep() {
    if (!state.playing) return state.step;
    var elapsedSec = Date.now() / 1000 - state.timestampSec;
    return state.step + elapsedSec * state.fps;
  }

  function applySync() {
    var step = currentStep();
    for (var trackId in state.tracks) {
      var track = state.tracks[trackId];
      var durationSec = track.durationSec;
      var offsetSec = (step - track.startStep) / state.fps;

      if (!(offsetSec >= 0 && offsetSec < durationSec)) {
        track.audio.pause();
        continue;
      }

      try {
        track.audio.currentTime = Math.max(0, Math.min(offsetSec, durationSec));
      } catch (err) {
        continue;
      }

      if (state.playing) {
        track.audio.play().then(function () {
          state.needsResume = false;
        }).catch(function () {});
      } else {
        track.audio.pause();
      }
    }
  }

  function upsertTrack(cmd) {
    var existing = state.tracks[cmd.track_id];
    if (existing) releaseTrack(existing);

    var bytes = decodeBase64ToBytes(cmd.wav_base64);
    var blob = new Blob([bytes], { type: "audio/wav" });
    var url = URL.createObjectURL(blob);
    var audio = new Audio(url);
    audio.preload = "auto";

    state.tracks[cmd.track_id] = {
      audio: audio,
      url: url,
      startStep: cmd.start_step,
      durationSec: cmd.num_frames / cmd.sample_rate,
    };

    applySync();
  }

  function syncPlayback(cmd) {
    state.playing = cmd.playing;
    state.step = cmd.step;
    state.fps = cmd.fps;
    state.timestampSec = cmd.timestamp_sec;
    applySync();
  }

  function dispatch(cmd) {
    if (cmd.kind === "upsert_track") {
      upsertTrack(cmd);
      return;
    }
    if (cmd.kind === "sync") {
      syncPlayback(cmd);
      return;
    }
  }

  document.addEventListener("pointerdown", function () {
    if (state.needsResume && state.playing) {
      applySync();
    }
  });

  window.__viser4d_audio = {
    dispatch: dispatch,
  };
})();
"""


class AudioManager:
    """Manages audio tracks and per-client playback synchronization."""

    def __init__(self, server: Viser4dServer) -> None:
        self._server = server
        self._lock = threading.RLock()

        self._tracks: dict[str, AudioTrack] = {}
        self._next_track_index = 0

        self._runtime_sent_client_ids: set[int] = set()

        self._playing = False
        self._step = 0
        self._fps = 30.0
        self._timestamp_sec = time.time()

        server.on_client_connect(self._on_client_connect)
        server.on_client_disconnect(self._on_client_disconnect)

    def add_track(self, audio: np.ndarray, sample_rate: int, start_step: int) -> str:
        """Encode audio data and register a track at ``start_step``."""
        if not isinstance(sample_rate, (int, np.integer)) or int(sample_rate) <= 0:
            raise ValueError("sample_rate must be a positive integer.")

        pcm16 = _coerce_audio_to_pcm16(audio)
        sample_rate_int = int(sample_rate)
        wav_bytes = _encode_wav_pcm16(pcm16, sample_rate_int)

        num_frames, num_channels = pcm16.shape
        wav_base64 = base64.b64encode(wav_bytes).decode("ascii")

        with self._lock:
            track_id = f"audio-{self._next_track_index}"
            self._next_track_index += 1
            track = AudioTrack(
                track_id=track_id,
                wav_base64=wav_base64,
                start_step=start_step,
                sample_rate=sample_rate_int,
                num_frames=num_frames,
                num_channels=num_channels,
            )
            self._tracks[track_id] = track

            clients = list(self._server.get_clients().values())

        for client in clients:
            self._ensure_runtime_sent(client)
            self._send_command(
                client,
                {
                    "kind": "upsert_track",
                    "track_id": track.track_id,
                    "wav_base64": track.wav_base64,
                    "start_step": track.start_step,
                    "sample_rate": track.sample_rate,
                    "num_frames": track.num_frames,
                    "num_channels": track.num_channels,
                },
                flush=False,
            )

        return track_id

    def on_play(self, current_step: int, fps: float) -> None:
        with self._lock:
            self._playing = True
            self._step = current_step
            self._fps = fps
            self._timestamp_sec = time.time()
            payload = self._make_sync_payload()

        self._broadcast_sync(payload)

    def on_pause(self, current_step: int, fps: float) -> None:
        with self._lock:
            self._playing = False
            self._step = current_step
            self._fps = fps
            self._timestamp_sec = time.time()
            payload = self._make_sync_payload()

        self._broadcast_sync(payload)

    def on_seek(self, step: int, fps: float) -> None:
        with self._lock:
            self._playing = False
            self._step = step
            self._fps = fps
            self._timestamp_sec = time.time()
            payload = self._make_sync_payload()

        self._broadcast_sync(payload)

    def _on_client_connect(self, client: ClientHandle) -> None:
        with self._lock:
            tracks = list(self._tracks.values())
            payload = self._make_sync_payload()

        self._ensure_runtime_sent(client)
        for track in tracks:
            self._send_command(
                client,
                {
                    "kind": "upsert_track",
                    "track_id": track.track_id,
                    "wav_base64": track.wav_base64,
                    "start_step": track.start_step,
                    "sample_rate": track.sample_rate,
                    "num_frames": track.num_frames,
                    "num_channels": track.num_channels,
                },
                flush=False,
            )
        self._send_command(client, payload, flush=True)

    def _on_client_disconnect(self, client: ClientHandle) -> None:
        with self._lock:
            self._runtime_sent_client_ids.discard(client.client_id)

    def _make_sync_payload(self) -> dict[str, object]:
        return {
            "kind": "sync",
            "playing": self._playing,
            "step": self._step,
            "fps": self._fps,
            "timestamp_sec": self._timestamp_sec,
        }

    def _broadcast_sync(self, payload: dict[str, object]) -> None:
        clients = list(self._server.get_clients().values())
        for client in clients:
            self._ensure_runtime_sent(client)
            self._send_command(client, payload, flush=True)

    def _ensure_runtime_sent(self, client: ClientHandle) -> None:
        with self._lock:
            if client.client_id in self._runtime_sent_client_ids:
                return
            self._runtime_sent_client_ids.add(client.client_id)

        client._websock_connection.queue_message(
            _viser_messages.RunJavascriptMessage(source=_AUDIO_RUNTIME_JS)
        )

    def _send_command(
        self,
        client: ClientHandle,
        payload: dict[str, object],
        *,
        flush: bool,
    ) -> None:
        source = (
            "window.__viser4d_audio.dispatch("
            + json.dumps(payload, separators=(",", ":"))
            + ");"
        )
        client._websock_connection.queue_message(
            _viser_messages.RunJavascriptMessage(source=source)
        )
        if flush:
            client.flush()


def _coerce_audio_to_pcm16(audio: np.ndarray) -> np.ndarray:
    if not isinstance(audio, np.ndarray):
        raise TypeError("audio must be a numpy.ndarray.")
    if audio.dtype not in (np.int16, np.float32):
        raise TypeError("audio dtype must be int16 or float32.")

    if audio.ndim == 1:
        samples = audio.reshape(-1, 1)
    elif audio.ndim == 2:
        samples = audio
    else:
        raise ValueError("audio must be a 1D or 2D array.")

    if samples.size == 0:
        raise ValueError("audio cannot be empty.")

    if samples.dtype == np.float32:
        clipped = np.clip(samples, -1.0, 1.0)
        pcm16 = (clipped * np.float32(32767.0)).astype("<i2")
    else:
        pcm16 = samples.astype("<i2", copy=False)

    return np.ascontiguousarray(pcm16)


def _encode_wav_pcm16(pcm16: np.ndarray, sample_rate: int) -> bytes:
    num_frames, num_channels = pcm16.shape
    payload = pcm16.tobytes()
    block_align = num_channels * 2
    byte_rate = sample_rate * block_align
    chunk_size = 36 + len(payload)
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF",
        chunk_size,
        b"WAVE",
        b"fmt ",
        16,  # PCM format chunk size
        1,  # PCM
        num_channels,
        sample_rate,
        byte_rate,
        block_align,
        16,  # bits per sample
        b"data",
        len(payload),
    )
    assert num_frames >= 0
    return header + payload
