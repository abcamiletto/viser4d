"""``Viser4dServer``: a viser server with a recorded, replayable time dimension.

The server owns the timeline, the recorder and one ``ClientSession`` per
connected browser, and is the only place that knows about all three: sessions
and the recorder receive exactly what they need through their constructors.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Coroutine
from contextlib import AbstractContextManager
from typing import Any

import viser
import viser.infra

from . import _export, _viser
from ._build import runtime_source
from ._config import PlaybackConfig, StreamingConfig, require_positive_float
from ._playback import ClientSession
from ._protocol import (
    EVENT_MESSAGE_TYPES,
    TimelineBlockMessage,
    TimelineEventMessage,
    TimelineReadyMessage,
)
from ._recorder import Recorder, TimelineContext
from ._state import SceneEntryRecord
from ._timeline import Timeline
from ._viser import ClientHandle

_TimestepCallback = Callable[[ClientHandle, int], None | Coroutine[Any, Any, None]]
_PlaybackCallback = Callable[[ClientHandle, bool], None | Coroutine[Any, Any, None]]

_REFRESH_DELAY = 0.05


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
        self._playback = PlaybackConfig(
            fps=require_positive_float("fps", fps),
            streaming=StreamingConfig.from_env() if streaming is None else streaming,
            loop=loop,
            speed=require_positive_float("playback_speed", playback_speed),
        )
        super().__init__(**kwargs)

        self._timeline = Timeline(num_steps, block_size=self.block_size)
        self._sessions: dict[int, ClientSession] = {}
        self._pending_ready_ids: set[int] = set()
        self._sessions_lock = threading.Lock()
        self._timestep_callbacks: list[_TimestepCallback] = []
        self._playback_callbacks: list[_PlaybackCallback] = []
        self._stop_event = threading.Event()
        self._refresh_lock = threading.Lock()
        self._refresh_timer: threading.Timer | None = None
        self._pending_refresh_block: int | None = None
        self._recorder = Recorder(
            self,
            self._timeline,
            on_override=self._broadcast_overrides,
            on_block_change=self._queue_block_refresh,
        )

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
        return self._playback.fps

    @property
    def streaming(self) -> StreamingConfig:
        return self._playback.streaming

    @property
    def block_size(self) -> int:
        return self._playback.streaming.block_size

    @property
    def loop(self) -> bool:
        return self._playback.loop

    @property
    def playback_speed(self) -> float:
        return self._playback.speed

    # -- playback controls ------------------------------------------------

    def play(self) -> None:
        for session in self.get_client_playbacks().values():
            session.play()

    def pause(self) -> None:
        for session in self.get_client_playbacks().values():
            session.pause()

    def refresh(self) -> None:
        for session in self.get_client_playbacks().values():
            session.refresh()

    def set_playback_speed(self, speed: float) -> None:
        speed = require_positive_float("speed", speed)
        self._playback.speed = speed
        for session in self.get_client_playbacks().values():
            session.set_speed(speed)

    def set_loop(self, loop: bool) -> None:
        self._playback.loop = loop
        for session in self.get_client_playbacks().values():
            session.sync_config()

    def set_steps(self, num_steps: int) -> None:
        if num_steps < 1:
            raise ValueError(f"num_steps must be >= 1, got {num_steps}.")
        if num_steps == self.num_steps:
            return
        self._recorder.require_idle(
            "server.set_steps() cannot run while inside server.at(t)."
        )
        self._cancel_refresh()
        self._timeline.resize(num_steps)
        for session in self.get_client_playbacks().values():
            session.sync_steps()

    def clear(self) -> None:
        self._recorder.require_idle(
            "server.clear() cannot run while inside server.at(t)."
        )
        self._cancel_refresh()
        self._timeline.clear()
        self.scene.reset()
        for session in self.get_client_playbacks().values():
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
        return self._export_serializer(start_timestep, end_timestep).serialize()

    def as_html(
        self,
        *,
        dark_mode: bool = False,
        start_timestep: int = 0,
        end_timestep: int | None = None,
    ) -> str:
        return self._export_serializer(start_timestep, end_timestep).as_html(
            dark_mode=dark_mode
        )

    def _export_serializer(
        self, start: int, end: int | None
    ) -> viser.infra.StateSerializer:
        return _export.build(
            self.get_scene_serializer(), self._timeline, self.fps, start, end
        )

    # -- lifecycle --------------------------------------------------------

    def sleep_forever(self) -> None:
        while not self._stop_event.wait(3600):
            pass

    def stop(self) -> None:
        self._stop_event.set()
        self._cancel_refresh()
        self._timeline.close()
        super().stop()

    # -- session registry -------------------------------------------------

    def _attach_session(self, client: ClientHandle) -> None:
        session = ClientSession(
            client,
            self._timeline,
            self._playback,
            executor=_viser.server_thread_executor(self),
            event_loop=self.get_event_loop(),
            on_timestep=self._dispatch_timestep_change,
            on_playback=self._dispatch_playback_change,
        )
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

    def _broadcast_overrides(self, entries: list[SceneEntryRecord]) -> None:
        for session in self.get_client_playbacks().values():
            for entry in entries:
                session.apply_override(entry)

    # -- debounced block resend -------------------------------------------

    def _queue_block_refresh(self, changed_block: int) -> None:
        """Coalesce block resends: recording a step usually rewrites a block
        that clients already hold, and bursts of ``at(t)`` calls are common."""
        with self._refresh_lock:
            pending = self._pending_refresh_block
            if pending is None or changed_block < pending:
                self._pending_refresh_block = changed_block
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
            self._refresh_timer = threading.Timer(
                _REFRESH_DELAY, self._flush_block_refreshes
            )
            self._refresh_timer.daemon = True
            self._refresh_timer.start()

    def _flush_block_refreshes(self) -> None:
        with self._refresh_lock:
            changed = self._pending_refresh_block
            self._pending_refresh_block = None
            self._refresh_timer = None
        if changed is None:
            return
        sessions = list(self.get_client_playbacks().values())
        messages: dict[int, TimelineBlockMessage] = {}
        for session in sessions:
            for index in sorted(session.loaded_blocks):
                if index < changed:
                    continue
                message = messages.get(index)
                if message is None:
                    message = self._timeline.block_message(index)
                    messages[index] = message
                session.update_block(index, message)
        # block_message() refreshes byte sizes, so snapshot them afterward.
        block_bytes = self._timeline.block_bytes()
        for session in sessions:
            session.send_block_bytes(block_bytes)

    def _cancel_refresh(self) -> None:
        with self._refresh_lock:
            timer = self._refresh_timer
            self._refresh_timer = None
            self._pending_refresh_block = None
        if timer is not None:
            timer.cancel()
