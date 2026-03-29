from __future__ import annotations

import inspect
import threading
from collections.abc import Awaitable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Callable

import viser

from . import _viser_private as impl
from .audio import AudioApi
from ._export import ExportBuilder
from ._types import RuntimeMethod, RuntimePayload
from ._runtime import make_runtime_message, runtime_source
from ._validation import require_positive_float
from .timeline._playback import ClientPlaybackHandle
from .timeline._recording import SceneRecorder, TimelineContext
from .timeline._store import TimelineStore

if TYPE_CHECKING:
    from ._viser_private import ClientHandle


class Viser4dServer(viser.ViserServer):
    """Viser server with timestep recording, playback, and synced audio."""

    # Override the parent's plain ``scene`` attribute with a property so that
    # ``server.scene`` transparently returns the timeline-owned scene API while
    # inside a ``server.at(t)`` block.  Outside of ``at()``, it returns the
    # regular live scene.
    @property
    def scene(self) -> impl.SceneApi:
        recorder = self.__dict__.get("_recorder")
        if recorder is not None and recorder.active_step is not None:
            return recorder.scene
        return self._live_scene

    @scene.setter
    def scene(self, value: impl.SceneApi) -> None:
        self._live_scene = value

    def __init__(self, num_steps: int, fps: float = 30.0, **kwargs) -> None:
        """Initialize the timeline runtime and client playback state."""
        if num_steps < 1:
            raise ValueError(f"num_steps must be >= 1, got {num_steps}.")
        super().__init__(**kwargs)

        self.num_steps = num_steps
        self._timeline_fps = require_positive_float("fps", fps)
        self._timeline = TimelineStore(
            self.num_steps,
            flush_executor=impl.server_thread_executor(self),
        )
        self._client_playbacks: dict[int, ClientPlaybackHandle] = {}
        self._client_playbacks_lock = threading.Lock()
        self._timestep_callbacks: list[
            Callable[[ClientHandle, int], None | Awaitable[None]]
        ] = []
        self._playback_callbacks: list[
            Callable[[ClientHandle, bool], None | Awaitable[None]]
        ] = []
        self._stop_event = threading.Event()
        self._recorder = SceneRecorder(self, self._timeline)
        self._export_builder = ExportBuilder(self, self._timeline)
        self.audio = AudioApi(self)

        # Load the browser runtime once so live clients can handle timeline/audio messages.
        impl.queue_server_message(self, impl.run_javascript_message(runtime_source()))

        @self.on_client_connect
        def _attach_playback(client: ClientHandle) -> None:
            playback = ClientPlaybackHandle(
                self,
                client,
                brand_color=impl.playback_brand_color(self),
            )
            with self._client_playbacks_lock:
                self._client_playbacks[client.client_id] = playback

        @self.on_client_disconnect
        def _detach_playback(client: ClientHandle) -> None:
            with self._client_playbacks_lock:
                self._client_playbacks.pop(client.client_id, None)

    def at(self, t: int) -> AbstractContextManager[TimelineContext]:
        """Expose the timeline APIs for timestep ``t``.

        Existing code can keep using ``server.scene`` inside the context. The
        returned ``timeline`` object provides the same timeline-owned scene and
        audio APIs explicitly.
        """
        return self._recorder.at(t)

    @property
    def fps(self) -> float:
        """Timeline step rate used for recording, audio timing, and export."""
        return self._timeline_fps

    def play(self, speed: float | None = None, loop: bool | None = None) -> None:
        """Ask connected clients to play from their own current timesteps."""
        next_speed = None
        if speed is not None:
            next_speed = require_positive_float("speed", speed)
        for playback in self._client_playback_values():
            playback.play(speed=next_speed, loop=loop)

    def pause(self) -> None:
        """Ask connected clients to pause at their current timesteps."""
        for playback in self._client_playback_values():
            playback.pause()

    def refresh(self) -> None:
        """Redraw the current timestep on all connected clients."""
        for playback in self._client_playback_values():
            playback.refresh()

    def set_playback_speed(self, speed: float) -> None:
        """Update connected client playback speed without starting playback."""
        next_speed = require_positive_float("speed", speed)
        for playback in self._client_playback_values():
            playback.set_speed(next_speed)

    def on_timestep_change(
        self,
        callback: Callable[[ClientHandle, int], None | Awaitable[None]],
    ) -> None:
        """Register a callback for any committed client timestep change."""
        self._timestep_callbacks.append(callback)

    def on_playback_change(
        self,
        callback: Callable[[ClientHandle, bool], None | Awaitable[None]],
    ) -> None:
        """Register a callback for client play/pause state changes."""
        self._playback_callbacks.append(callback)

    def get_client_playback(self, client_id: int) -> ClientPlaybackHandle | None:
        """Return one connected client playback handle, if present."""
        with self._client_playbacks_lock:
            return self._client_playbacks.get(client_id)

    def get_client_playbacks(self) -> dict[int, ClientPlaybackHandle]:
        """Return a copy of the connected client playback handles."""
        with self._client_playbacks_lock:
            return self._client_playbacks.copy()

    def sleep_forever(self) -> None:
        """Block until the server is stopped."""
        while not self._stop_event.wait(3600):
            pass

    def serialize(
        self,
        *,
        start_timestep: int = 0,
        end_timestep: int | None = None,
    ) -> bytes:
        """Serialize the recorded timeline to bytes."""
        return self._export_builder.serialize(
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        )

    def stop(self) -> None:
        """Shut down the underlying viser server."""
        self._stop_event.set()
        self._recorder.close()
        self._timeline.close()
        super().stop()

    def _dispatch_audio_update(self, message: impl.Message) -> None:
        self._recorder.dispatch_audio_update(message)

    def _send_runtime_call(
        self, method: RuntimeMethod, payload: RuntimePayload
    ) -> None:
        message = make_runtime_message(method, payload)
        impl.queue_server_message(self, message)

    def _client_playback_values(self) -> list[ClientPlaybackHandle]:
        with self._client_playbacks_lock:
            return list(self._client_playbacks.values())

    def _dispatch_timestep_change(self, client: ClientHandle, timestep: int) -> None:
        for callback in list(self._timestep_callbacks):
            maybe_awaitable = callback(client, timestep)
            if inspect.isawaitable(maybe_awaitable):
                self.get_event_loop().create_task(_await_callback(maybe_awaitable))

    def _dispatch_playback_change(self, client: ClientHandle, is_playing: bool) -> None:
        for callback in list(self._playback_callbacks):
            maybe_awaitable = callback(client, is_playing)
            if inspect.isawaitable(maybe_awaitable):
                self.get_event_loop().create_task(_await_callback(maybe_awaitable))


async def _await_callback(awaitable: Awaitable[None]) -> None:
    await awaitable
