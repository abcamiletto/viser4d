from __future__ import annotations

import contextlib
import pathlib
import threading
from types import MethodType
from typing import TYPE_CHECKING, Callable, Iterator, cast

import numpy as np
import viser
from viser import _messages
from viser.infra import Message

from ._audio import AudioHandle
from ._controller import TimelineController
from ._export import ExportBuilder
from ._playback import ClientPlaybackHandle
from ._protocol import RuntimeMethod, RuntimePayload
from ._recording import SceneRecorder
from ._runtime import make_runtime_message, runtime_source
from ._timeline import TimelineStore
from ._viser_monkeypatch import ensure_viser_audio_patch

if TYPE_CHECKING:
    from viser._viser import ClientHandle

    class Viser4dSceneApi(viser.SceneApi):
        def add_audio(
            self, name: str, *, data: np.ndarray, sample_rate: int
        ) -> AudioHandle: ...


class Viser4dServer(viser.ViserServer):
    """Viser server with timestep recording, playback, and synced audio."""

    if TYPE_CHECKING:
        scene: Viser4dSceneApi

    def __init__(self, num_steps: int, fps: float = 30.0, **kwargs) -> None:
        num_steps = int(num_steps)
        if num_steps < 1:
            raise ValueError(f"num_steps must be >= 1, got {num_steps}.")
        ensure_viser_audio_patch()
        super().__init__(**kwargs)

        self.num_steps = num_steps
        self._timeline = TimelineStore(self.num_steps)
        self._client_playbacks: dict[int, ClientPlaybackHandle] = {}
        self._client_playbacks_lock = threading.Lock()
        self._playback_brand_color: tuple[int, int, int] | None = None
        self._stop_event = threading.Event()
        setattr(self.scene, "add_audio", MethodType(_scene_add_audio, self.scene))
        self._install_theme_tracking()
        self._controller = TimelineController(self, fps=fps)
        self._recorder = SceneRecorder(self, self._timeline)
        self._export_builder = ExportBuilder(self, self._timeline)

        self._websock_server.queue_message(
            _messages.RunJavascriptMessage(runtime_source())
        )

        @self.on_client_connect
        def _attach_playback(client: ClientHandle) -> None:
            playback = ClientPlaybackHandle(self, client)
            with self._client_playbacks_lock:
                self._client_playbacks[client.client_id] = playback
            playback.apply_theme_colors(self._playback_brand_color)

        @self.on_client_disconnect
        def _detach_playback(client: ClientHandle) -> None:
            with self._client_playbacks_lock:
                self._client_playbacks.pop(client.client_id, None)

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[None]:
        """Record scene and audio operations for timestep ``t``."""
        with self._recorder.at(t):
            yield

    def play(self, fps: float, loop: bool = False) -> None:
        """Start timeline playback at ``fps`` and optionally loop."""
        self._controller.play(fps, loop=loop)
        for playback in self._client_playback_values():
            playback.play(fps, loop=loop)

    def pause(self) -> None:
        self._controller.pause()
        for playback in self._client_playback_values():
            playback.pause()

    def seek(self, t: int) -> None:
        self._controller.seek(t)
        timestep = self._controller.current_timestep
        for playback in self._client_playback_values():
            playback.seek(timestep)

    def set_fps(self, fps: float) -> None:
        self._controller.set_fps(fps)
        for playback in self._client_playback_values():
            playback.set_fps(self._controller.fps)

    def on_timestep_change(self, callback: Callable[[int], None]) -> None:
        self._controller.on_timestep_change(callback)

    def sleep_forever(self) -> None:
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

    def write_recording(
        self,
        path: str | pathlib.Path,
        *,
        start_timestep: int = 0,
        end_timestep: int | None = None,
    ) -> None:
        """Write the recorded timeline to ``path``."""
        self._export_builder.write(
            path,
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        )

    def stop(self) -> None:
        self._stop_event.set()
        self._controller.stop()
        super().stop()

    def _add_audio(
        self, name: str, *, data: np.ndarray, sample_rate: int
    ) -> AudioHandle:
        if self._recorder.active_step is None:
            raise RuntimeError("add_audio() is only valid inside server.at(t).")
        return self._recorder.add_audio(name, data=data, sample_rate=sample_rate)

    def _dispatch_audio_update(self, message: Message) -> None:
        self._recorder.dispatch_audio_update(message)

    def _send_runtime_call(
        self, method: RuntimeMethod, payload: RuntimePayload
    ) -> None:
        message = make_runtime_message(method, payload)
        self._websock_server.queue_message(message)

    def _install_theme_tracking(self) -> None:
        original_configure_theme = self.gui.configure_theme

        def configure_theme_wrapper(*args, **kwargs) -> None:
            original_configure_theme(*args, **kwargs)
            self._playback_brand_color = _primary_brand_color(
                kwargs.get("brand_color")
            )
            for playback in self._client_playback_values():
                playback.apply_theme_colors(self._playback_brand_color)

        setattr(self.gui, "configure_theme", configure_theme_wrapper)

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
        return self._controller.current_timestep


def _scene_add_audio(
    scene: viser.SceneApi,
    name: str,
    *,
    data: np.ndarray,
    sample_rate: int,
) -> AudioHandle:
    server = cast(Viser4dServer, scene._owner)
    return server._add_audio(name, data=data, sample_rate=sample_rate)


def _primary_brand_color(
    brand_color: tuple[int, int, int] | tuple[str, ...] | None,
) -> tuple[int, int, int] | None:
    if brand_color is None:
        return None
    if len(brand_color) == 3:
        return cast(tuple[int, int, int], brand_color)
    if len(brand_color) == 10:
        return _hex_to_rgb(cast(tuple[str, ...], brand_color)[8])
    return None


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    assert len(color) == 6
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
