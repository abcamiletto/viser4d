from __future__ import annotations

import contextlib
import inspect
import threading
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Callable, Iterator

import viser
from viser import _messages

from . import _viser_private as impl
from .audio import AudioApi
from ._export import ExportBuilder
from ._types import RuntimeMethod, RuntimePayload
from ._runtime import make_runtime_message, runtime_source
from .timeline import (
    ClientPlaybackHandle,
    SceneRecorder,
    TimelineStore,
)

if TYPE_CHECKING:
    from viser._viser import ClientHandle


class Viser4dServer(viser.ViserServer):
    """Viser server with timestep recording, playback, and synced audio."""

    def __init__(self, num_steps: int, fps: float = 30.0, **kwargs) -> None:
        """Initialize the timeline runtime and client playback state."""
        if num_steps < 1:
            raise ValueError(f"num_steps must be >= 1, got {num_steps}.")
        super().__init__(**kwargs)

        self.num_steps = num_steps
        self._fps = float(fps)
        self._timeline_fps = float(fps)
        self._timeline = TimelineStore(self.num_steps)
        self._client_playbacks: dict[int, ClientPlaybackHandle] = {}
        self._client_playbacks_lock = threading.Lock()
        self._timestep_callbacks: list[
            Callable[[ClientHandle, int], None | Awaitable[None]]
        ] = []
        self._stop_event = threading.Event()
        self._recorder = SceneRecorder(self, self._timeline)
        self._export_builder = ExportBuilder(self, self._timeline)
        self.audio = AudioApi(self)

        # Load the browser runtime once so live clients can handle timeline/audio messages.
        impl.queue_server_message(
            self, _messages.RunJavascriptMessage(runtime_source())
        )

        @self.on_client_connect
        def _attach_playback(client: ClientHandle) -> None:
            playback = ClientPlaybackHandle(self, client)
            with self._client_playbacks_lock:
                self._client_playbacks[client.client_id] = playback
            playback.apply_theme_colors(impl.playback_brand_color(self))

        @self.on_client_disconnect
        def _detach_playback(client: ClientHandle) -> None:
            with self._client_playbacks_lock:
                self._client_playbacks.pop(client.client_id, None)

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[None]:
        """Record scene and audio operations for timestep ``t``."""
        with self._recorder.at(t):
            yield

    @property
    def fps(self) -> float:
        """Default client playback speed for connected and future clients."""
        return self._fps

    def play(self, fps: float | None = None, loop: bool = False) -> None:
        """Ask connected clients to play from their own current timesteps."""
        if fps is not None:
            self._fps = float(fps)
        for playback in self._client_playback_values():
            playback.play(self._fps, loop=loop)

    def pause(self) -> None:
        """Ask connected clients to pause at their current timesteps."""
        for playback in self._client_playback_values():
            playback.pause()

    def refresh(self) -> None:
        """Redraw the current timestep on all connected clients."""
        for playback in self._client_playback_values():
            playback.refresh()

    def set_fps(self, fps: float) -> None:
        """Update client playback speed without changing timeline cadence."""
        self._fps = float(fps)
        for playback in self._client_playback_values():
            playback.set_fps(self._fps)

    def on_timestep_change(
        self,
        callback: Callable[[ClientHandle, int], None | Awaitable[None]],
    ) -> None:
        """Register a callback for any committed client timestep change."""
        self._timestep_callbacks.append(callback)

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
        super().stop()

    def _dispatch_audio_update(self, message: _messages.Message) -> None:
        self._recorder.dispatch_audio_update(message)

    def _send_runtime_call(
        self, method: RuntimeMethod, payload: RuntimePayload
    ) -> None:
        message = make_runtime_message(method, payload)
        impl.queue_server_message(self, message)

    def _client_playback_values(self) -> list[ClientPlaybackHandle]:
        with self._client_playbacks_lock:
            return list(self._client_playbacks.values())

    def _dispatch_timestep_change(
        self, client: ClientHandle, timestep: int
    ) -> None:
        for callback in list(self._timestep_callbacks):
            maybe_awaitable = callback(client, timestep)
            if inspect.iscoroutine(maybe_awaitable):
                self._event_loop.create_task(maybe_awaitable)
