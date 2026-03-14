from __future__ import annotations

import contextlib
import threading
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
    TimelineController,
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
        self._timeline = TimelineStore(self.num_steps)
        self._client_playbacks: dict[int, ClientPlaybackHandle] = {}
        self._client_playbacks_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._controller = TimelineController(self, fps=fps)
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

    def play(self, fps: float | None = None, loop: bool = False) -> None:
        """Start timeline playback at the current FPS and optionally loop."""
        fps = self._controller.fps if fps is None else float(fps)
        self._controller.play(fps, loop=loop)
        for playback in self._client_playback_values():
            playback.play(fps, loop=loop)

    def pause(self) -> None:
        """Pause timeline playback for all connected clients."""
        self._controller.pause()
        for playback in self._client_playback_values():
            playback.pause()

    def seek(self, t: int) -> None:
        """Jump the timeline to timestep ``t``."""
        self._controller.seek(t)
        timestep = self._controller.current_timestep
        for playback in self._client_playback_values():
            playback.seek(timestep)

    def refresh(self) -> None:
        """Redraw the current timestep on all connected clients while paused."""
        if self._is_playing:
            return
        for playback in self._client_playback_values():
            playback.refresh()

    def set_fps(self, fps: float) -> None:
        """Update playback speed without changing the current timestep."""
        self._controller.set_fps(fps)
        for playback in self._client_playback_values():
            playback.set_fps(self._controller.fps)

    def on_timestep_change(self, callback: Callable[[int], None]) -> None:
        """Register a callback for committed timestep changes."""
        self._controller.on_timestep_change(callback)

    @property
    def current_timestep(self) -> int:
        """Return the current discrete timestep."""
        return self._controller.current_timestep

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
        """Stop the predictor thread and shut down the underlying viser server."""
        self._stop_event.set()
        self._controller.stop()
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

    def _sync_client_playback_state(
        self,
        *,
        timestep: int | None = None,
        fps: float | None = None,
        loop: bool | None = None,
        is_playing: bool | None = None,
    ) -> None:
        for playback in self._client_playback_values():
            playback.sync_state(
                timestep=timestep,
                fps=fps,
                loop=loop,
                is_playing=is_playing,
            )

    @property
    def _fps(self) -> float:
        return self._controller.fps

    @property
    def _base_fps(self) -> float:
        return self._controller.base_fps

    @property
    def _loop(self) -> bool:
        return self._controller.loop

    @property
    def _is_playing(self) -> bool:
        return self._controller.is_playing

    @property
    def _current_timestep(self) -> int:
        return self.current_timestep
