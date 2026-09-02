"""The recording seam: a shadow ``SceneApi`` whose transport routes messages.

Messages produced inside ``server.at(t)`` fold into that timestep's delta.
Messages produced outside it become overrides: a keyed overlay applied on top of
every step, which is the only out-of-session write path (creating nodes or
editing audio outside ``at(t)`` raises). A shadow ``SceneApi`` gives callers the
full viser scene API while its fake transport feeds every emitted message back
here instead of onto a websocket.
"""

from __future__ import annotations

import contextlib
import dataclasses
import threading
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np
import viser

from . import _audio, _viser
from ._state import SceneEntryRecord
from ._timeline import Timeline


@dataclasses.dataclass(frozen=True)
class TimelineContext:
    """Scene and audio APIs exposed inside ``server.at(t)``."""

    scene: _viser.SceneApi
    audio: _audio.AudioApi


@dataclasses.dataclass
class _WriteSession:
    step: int
    messages: list[_viser.Message] = dataclasses.field(default_factory=list)


class Recorder:
    """Capture scene and audio edits into the timeline."""

    def __init__(
        self,
        server: viser.ViserServer,
        timeline: Timeline,
        *,
        on_override: Callable[[list[SceneEntryRecord]], None],
        on_block_change: Callable[[int], None],
    ) -> None:
        self._timeline = timeline
        self._on_override = on_override
        self._on_block_change = on_block_change
        self._live_scene = server.scene
        self._lock = threading.RLock()
        self._active: _WriteSession | None = None

        transport = _TimelineTransport(server)
        self.scene = _viser.create_scene_api(
            _TimelineSceneOwner(transport),
            thread_executor=_viser.server_thread_executor(server),
            event_loop=server.get_event_loop(),
        )
        _viser.set_scene_owner(self.scene, server)
        self.audio = _audio.AudioApi(self)
        # SceneApi.__init__ pushes its own /WorldAxes frame through the
        # transport; attaching the recorder last discards that message.
        transport.recorder = self

    # -- recording sessions ----------------------------------------------

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[TimelineContext]:
        changed_block: int | None = None
        with self._lock:
            if self._active is not None:
                raise RuntimeError("server.at(t) cannot be nested.")
            session = _WriteSession(step=self._timeline.validate_step(t))
            self._active = session
            try:
                yield TimelineContext(scene=self.scene, audio=self.audio)
            finally:
                self._active = None
                if session.messages:
                    self._reject_static_collisions(session.messages)
                    self._timeline.record_step(session.step, session.messages)
                    changed_block = self._timeline.block_index_for_step(session.step)
        if changed_block is not None:
            self._on_block_change(changed_block)

    def require_idle(self, message: str) -> None:
        if self._active is not None:
            raise RuntimeError(message)

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
        self._on_override(self._timeline.record_override(message))

    def dispatch_audio(self, message: _viser.Message) -> None:
        if self._active is None:
            raise RuntimeError(
                "Timeline audio edits are only valid inside server.at(t)."
            )
        self._active.messages.append(message)

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

    def __init__(self, server: viser.ViserServer) -> None:
        super().__init__()
        self._server = server
        self.recorder: Recorder | None = None

    def get_message_buffer(self) -> Any:
        return self

    def push(self, message: _viser.Message) -> None:
        if self.recorder is not None:
            self.recorder.route_message(message)

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
