"""``Viser4dServer``: a viser server with a recorded, replayable time dimension."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from contextlib import AbstractContextManager
from typing import Any

import viser

from . import _viser
from ._build import runtime_source
from ._config import StreamingConfig, require_positive_float
from ._export import ExportBuilder
from ._playback import ClientSession
from ._protocol import EVENT_MESSAGE_TYPES, TimelineEventMessage, TimelineReadyMessage
from ._recorder import Recorder, TimelineContext
from ._timeline import Timeline
from ._viser import ClientHandle

_TimestepCallback = Callable[[ClientHandle, int], None | Coroutine[Any, Any, None]]
_PlaybackCallback = Callable[[ClientHandle, bool], None | Coroutine[Any, Any, None]]


class Viser4dServer(viser.ViserServer):
    """Viser server with timestep recording, playback, and synced audio."""

    def __init__(
        self,
        num_steps: int,
        fps: float = 30.0,
        *,
        streaming: StreamingConfig | None = None,
        loop: bool = False,
        playback_speed: float = 1.0,
        **kwargs: Any,
    ) -> None:
        if num_steps < 1:
            raise ValueError(f"num_steps must be >= 1, got {num_steps}.")
        self._timeline_fps = require_positive_float("fps", fps)
        self._streaming = StreamingConfig.from_env() if streaming is None else streaming
        self._default_speed = require_positive_float("playback_speed", playback_speed)
        super().__init__(**kwargs)

        self._config_lock = threading.Lock()
        self._loop = loop
        self._playback_speed = self._default_speed
        self._timeline = Timeline(num_steps, block_size=self._streaming.block_size)
        self._sessions: dict[int, ClientSession] = {}
        self._pending_ready_ids: set[int] = set()
        self._sessions_lock = threading.Lock()
        self._timestep_callbacks: list[_TimestepCallback] = []
        self._playback_callbacks: list[_PlaybackCallback] = []
        self._stop_event = threading.Event()
        self._recorder = Recorder(self)
        self._export = ExportBuilder(self)

        _viser.queue_server_message(
            self, _viser.run_javascript_message(runtime_source())
        )
        for message_cls in EVENT_MESSAGE_TYPES:
            _viser.register_message_handler(self, message_cls, self._handle_event)

        self.on_client_connect(self._attach_session)
        self.on_client_disconnect(self._detach_session)

    # -- recording --------------------------------------------------------

    def at(self, t: int) -> AbstractContextManager[TimelineContext]:
        """Record scene and audio mutations into timestep ``t`` (non-nestable)."""
        return self._recorder.at(t)

    # -- configuration ----------------------------------------------------

    @property
    def num_steps(self) -> int:
        return self._timeline.num_steps

    @property
    def fps(self) -> float:
        return self._timeline_fps

    @property
    def streaming(self) -> StreamingConfig:
        return self._streaming

    @property
    def block_size(self) -> int:
        return self._streaming.block_size

    @property
    def loop(self) -> bool:
        with self._config_lock:
            return self._loop

    @property
    def playback_speed(self) -> float:
        with self._config_lock:
            return self._playback_speed

    # -- playback controls ------------------------------------------------

    def play(self) -> None:
        for session in self._client_session_values():
            session.play()

    def pause(self) -> None:
        for session in self._client_session_values():
            session.pause()

    def refresh(self) -> None:
        for session in self._client_session_values():
            session.refresh()

    def set_playback_speed(self, speed: float) -> None:
        next_speed = require_positive_float("speed", speed)
        with self._config_lock:
            self._playback_speed = next_speed
        for session in self._client_session_values():
            session.set_speed(next_speed)

    def set_loop(self, loop: bool) -> None:
        with self._config_lock:
            self._loop = loop
        for session in self._client_session_values():
            session.sync_config()

    def set_steps(self, num_steps: int) -> None:
        if num_steps < 1:
            raise ValueError(f"num_steps must be >= 1, got {num_steps}.")
        if num_steps == self.num_steps:
            return
        self._recorder.resize(num_steps)
        for session in self._client_session_values():
            session.sync_steps()

    def clear(self) -> None:
        self._recorder.clear()
        self.scene.reset()
        for session in self._client_session_values():
            session.clear()

    # -- callbacks --------------------------------------------------------

    def on_timestep_change(self, callback: _TimestepCallback) -> None:
        self._timestep_callbacks.append(callback)

    def on_playback_change(self, callback: _PlaybackCallback) -> None:
        self._playback_callbacks.append(callback)

    def get_client_playback(self, client_id: int) -> ClientSession | None:
        with self._sessions_lock:
            return self._sessions.get(client_id)

    def get_client_playbacks(self) -> dict[int, ClientSession]:
        with self._sessions_lock:
            return dict(self._sessions)

    # -- export -----------------------------------------------------------

    def serialize(
        self, *, start_timestep: int = 0, end_timestep: int | None = None
    ) -> bytes:
        return self._export.serialize(
            start_timestep=start_timestep, end_timestep=end_timestep
        )

    def as_html(
        self,
        *,
        dark_mode: bool = False,
        start_timestep: int = 0,
        end_timestep: int | None = None,
    ) -> str:
        return self._export.as_html(
            dark_mode=dark_mode,
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        )

    # -- lifecycle --------------------------------------------------------

    def sleep_forever(self) -> None:
        while not self._stop_event.wait(3600):
            pass

    def stop(self) -> None:
        self._stop_event.set()
        self._recorder.close()
        self._timeline.close()
        super().stop()

    # -- internals --------------------------------------------------------

    def _client_session_values(self) -> list[ClientSession]:
        with self._sessions_lock:
            return list(self._sessions.values())

    def _attach_session(self, client: ClientHandle) -> None:
        session = ClientSession(self, client)
        # Register before the initial sync so overrides recorded concurrently
        # are forwarded; the session may then receive them twice (idempotent).
        with self._sessions_lock:
            self._sessions[client.client_id] = session
            replay = client.client_id in self._pending_ready_ids
            self._pending_ready_ids.discard(client.client_id)
        session.start()
        if replay:
            session.handle_event(TimelineReadyMessage())

    def _detach_session(self, client: ClientHandle) -> None:
        with self._sessions_lock:
            self._sessions.pop(client.client_id, None)
            self._pending_ready_ids.discard(client.client_id)

    def _handle_event(self, client_id: int, message: TimelineEventMessage) -> None:
        with self._sessions_lock:
            session = self._sessions.get(client_id)
            if session is None:
                if isinstance(message, TimelineReadyMessage):
                    self._pending_ready_ids.add(client_id)
                return
        session.handle_event(message)

    def _dispatch_timestep_change(self, client: ClientHandle, step: int) -> None:
        for callback in list(self._timestep_callbacks):
            result = callback(client, step)
            if asyncio.iscoroutine(result):
                self.get_event_loop().create_task(result)

    def _dispatch_playback_change(self, client: ClientHandle, is_playing: bool) -> None:
        for callback in list(self._playback_callbacks):
            result = callback(client, is_playing)
            if asyncio.iscoroutine(result):
                self.get_event_loop().create_task(result)
