"""The recording seam: a shadow ``SceneApi`` whose transport routes messages.

Messages produced inside ``server.at(t)`` fold into that timestep's delta;
messages produced outside it become live overrides. A shadow ``SceneApi`` gives
callers the full viser scene API while its fake transport feeds every emitted
message back here instead of onto a websocket.
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

import numpy as np

from . import _audio, _viser

if TYPE_CHECKING:
    from ._server import Viser4dServer


@dataclasses.dataclass(frozen=True)
class TimelineContext:
    """Scene and audio APIs exposed inside ``server.at(t)``."""

    scene: _viser.SceneApi
    audio: _audio.AudioApi


@dataclasses.dataclass
class _WriteSession:
    step: int
    messages: list[_viser.Message] = dataclasses.field(default_factory=list)


_REFRESH_DELAY = 0.05


class Recorder:
    """Capture scene and audio edits into the timeline."""

    def __init__(self, server: Viser4dServer) -> None:
        self._server = server
        self._live_scene = server.scene
        self._lock = threading.RLock()
        self._active: _WriteSession | None = None
        self._refresh_lock = threading.Lock()
        self._refresh_timer: threading.Timer | None = None
        self._pending_refresh_block: int | None = None

        self._transport = _TimelineTransport(server, self)
        self.scene = _viser.create_scene_api(
            _TimelineSceneOwner(self._transport),
            thread_executor=_viser.server_thread_executor(server),
            event_loop=server.get_event_loop(),
        )
        _viser.set_scene_owner(self.scene, server)
        self.audio = _audio.AudioApi(self)
        self._transport.start()

    # -- recording sessions ----------------------------------------------

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[TimelineContext]:
        changed_block: int | None = None
        with self._lock:
            if self._active is not None:
                raise RuntimeError("server.at(t) cannot be nested.")
            session = _WriteSession(step=self._server._timeline.validate_step(t))
            self._active = session
            try:
                yield TimelineContext(scene=self.scene, audio=self.audio)
            finally:
                self._active = None
                if session.messages:
                    self._reject_static_collisions(session.messages)
                    self._server._timeline.record_step(session.step, session.messages)
                    changed_block = self._server._timeline.block_index_for_step(
                        session.step
                    )
        if changed_block is not None:
            self._queue_block_refresh(changed_block)

    def add_audio(
        self, name: str, *, data: np.ndarray, sample_rate: int
    ) -> _audio.AudioHandle:
        if self._active is None:
            raise RuntimeError(
                "timeline.audio.add_track() is only valid inside server.at(t)."
            )
        buffer = _audio._TrackBuffer(name, sample_rate, data)
        self._active.messages.append(_audio.add_audio_message(buffer))
        return _audio.AudioHandle(self.dispatch_audio, buffer)

    # -- message routing --------------------------------------------------

    def route_message(self, message: _viser.Message) -> None:
        if self._active is not None:
            self._active.messages.append(message)
            return
        if _viser.is_create_scene_node_message(message):
            raise RuntimeError(
                "Timeline scene node creation is only valid inside server.at(t)."
            )
        changed = self._server._timeline.record_override(message)
        for session in self._server._client_session_values():
            for entry in changed:
                session.apply_override(entry)

    def dispatch_audio(self, message: _viser.Message) -> None:
        if self._active is None:
            raise RuntimeError(
                "Timeline audio edits are only valid inside server.at(t)."
            )
        self._active.messages.append(message)

    # -- resize / clear ---------------------------------------------------

    def resize(self, num_steps: int) -> None:
        self._require_idle("server.set_steps() cannot run while inside server.at(t).")
        self._server._timeline.resize(num_steps)

    def clear(self) -> None:
        self._require_idle("server.clear() cannot run while inside server.at(t).")
        self._server._timeline.clear()

    def _require_idle(self, message: str) -> None:
        self._cancel_refresh()
        if self._active is not None:
            raise RuntimeError(message)

    # -- debounced block resend ------------------------------------------

    def _queue_block_refresh(self, changed_block: int) -> None:
        with self._refresh_lock:
            pending = self._pending_refresh_block
            if pending is None or changed_block < pending:
                self._pending_refresh_block = changed_block
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
            timer = threading.Timer(_REFRESH_DELAY, self._flush_block_refreshes)
            timer.daemon = True
            self._refresh_timer = timer
            timer.start()

    def _flush_block_refreshes(self) -> None:
        with self._refresh_lock:
            changed = self._pending_refresh_block
            self._pending_refresh_block = None
            self._refresh_timer = None
        if changed is None:
            return
        timeline = self._server._timeline
        sessions = self._server._client_session_values()
        messages: dict[int, Any] = {}
        for session in sessions:
            for index in sorted(session.loaded_blocks):
                if index < changed:
                    continue
                message = messages.get(index)
                if message is None:
                    message = timeline.block_message(index)
                    messages[index] = message
                session.update_block(index, message)
        # block_message() refreshes byte sizes, so snapshot manifests afterward.
        manifests = timeline.block_manifests()
        for session in sessions:
            session.send_manifests(manifests)

    def _cancel_refresh(self) -> None:
        with self._refresh_lock:
            timer = self._refresh_timer
            self._refresh_timer = None
            self._pending_refresh_block = None
        if timer is not None:
            timer.cancel()

    def close(self) -> None:
        self._cancel_refresh()

    def _reject_static_collisions(self, messages: list[_viser.Message]) -> None:
        for message in messages:
            if _viser.is_create_scene_node_message(message) and _viser.scene_has_node(
                self._live_scene,
                message.name,  # type: ignore[attr-defined]
            ):
                raise RuntimeError(
                    f"Cannot create timeline node {message.name!r} because a static "  # type: ignore[attr-defined]
                    "scene node with the same name already exists."
                )


class _TimelineTransport(_viser.WebsockMessageHandler):
    """Fake transport that feeds ``SceneApi`` output back into the recorder."""

    def __init__(self, server: Viser4dServer, recorder: Recorder) -> None:
        super().__init__()
        self._server = server
        self._recorder = recorder
        self._started = False

    def start(self) -> None:
        self._started = True

    def get_message_buffer(self) -> Any:
        return self

    def push(self, message: _viser.Message) -> None:
        if self._started:
            self._recorder.route_message(message)

    def atomic_start(self) -> None:
        pass

    def atomic_end(self) -> None:
        pass

    def register_handler(self, message_cls: type[Any], callback: Any) -> None:
        _viser.register_message_handler(self._server, message_cls, callback)

    def unregister_handler(self, message_cls: type[Any], callback: Any = None) -> None:
        _viser.unregister_message_handler(self._server, message_cls, callback)


class _TimelineSceneOwner:
    """Minimal client-like owner required by ``SceneApi``."""

    def __init__(self, transport: _TimelineTransport) -> None:
        self._websock_connection = transport
