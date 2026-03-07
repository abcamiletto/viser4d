from __future__ import annotations

import contextlib
import pathlib
import signal
import threading
from types import MethodType
from typing import TYPE_CHECKING, Any, Callable, Iterator, cast

import numpy as np
import viser
from viser import _messages

from ._audio import AudioHandle
from ._controller import TimelineController
from ._export import ExportBuilder
from ._recording import SceneRecorder
from ._runtime import make_runtime_message, runtime_source
from ._timeline import TimelineStore

if TYPE_CHECKING:

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
        super().__init__(**kwargs)

        self.num_steps = num_steps
        self._timeline = TimelineStore(self.num_steps)
        setattr(self.scene, "add_audio", MethodType(_scene_add_audio, self.scene))
        self._controller = TimelineController(self, fps=fps)
        self._recorder = SceneRecorder(self, self._timeline)
        self._export_builder = ExportBuilder(self, self._timeline)

        self._websock_server.queue_message(
            _messages.RunJavascriptMessage(runtime_source())
        )
        self._timestep_sync = self.gui.add_number(
            "__viser4d_timestep_sync__",
            0,
            min=0,
            max=max(self.num_steps - 1, 0),
            step=1,
            visible=False,
        )
        with self.gui.add_folder("Playback"):
            self._timeline_slider = self.gui.add_slider(
                "Timestep",
                min=0,
                max=max(self.num_steps - 1, 0),
                step=1,
                initial_value=0,
            )
            self._fps_slider = self.gui.add_slider(
                "FPS",
                min=1.0,
                max=120.0,
                step=1.0,
                initial_value=self._controller.fps,
            )
            self._step_buttons = self.gui.add_button_group("Step", ("Prev", "Next"))
            self._play_button = self.gui.add_button(
                "Play",
                color="green",
                icon=viser.Icon.PLAYER_PLAY_FILLED,
            )
            self._pause_button = self.gui.add_button(
                "Pause",
                color="yellow",
                icon=viser.Icon.PLAYER_PAUSE_FILLED,
                visible=False,
            )

        @self._timestep_sync.on_update
        def _sync(_event: Any) -> None:
            self._controller.sync_from_client(int(self._timestep_sync.value))

        @self._timeline_slider.on_update
        def _sync_slider(_event: Any) -> None:
            if self._controller.syncing_timestep_slider:
                return
            self.seek(int(self._timeline_slider.value))

        @self._fps_slider.on_update
        def _sync_fps(_event: Any) -> None:
            self._controller.set_fps(float(self._fps_slider.value))

        @self._step_buttons.on_click
        def _step(_event: Any) -> None:
            if self._step_buttons.value == "Prev":
                self.seek(self._controller.current_timestep - 1)
            else:
                self.seek(self._controller.current_timestep + 1)

        @self._play_button.on_click
        def _play(_event: Any) -> None:
            self.play(self._controller.fps, loop=self._controller.loop)

        @self._pause_button.on_click
        def _pause(_event: Any) -> None:
            self.pause()

        self._controller.sync_runtime_config()

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[None]:
        """Record scene and audio operations for timestep ``t``."""
        with self._recorder.at(t):
            yield

    def play(self, fps: float, loop: bool = False) -> None:
        """Start timeline playback at ``fps`` and optionally loop."""
        self._controller.play(fps, loop=loop)

    def pause(self) -> None:
        self._controller.pause()

    def seek(self, t: int) -> None:
        self._controller.seek(t)

    def on_timestep_change(self, callback: Callable[[int], None]) -> None:
        self._controller.on_timestep_change(callback)

    def sleep_forever(self) -> None:
        if threading.current_thread() is threading.main_thread() and hasattr(
            signal, "pause"
        ):
            while True:
                signal.pause()

        sleeper = threading.Event()
        while True:
            sleeper.wait(3600)

    def serialize(
        self,
        path: str | pathlib.Path,
        *,
        start_timestep: int = 0,
        end_timestep: int = -1,
    ) -> bytes:
        """Write the recorded timeline to ``path`` and return its bytes."""
        return self._export_builder.serialize(
            path,
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        )

    def stop(self) -> None:
        self._controller.stop()
        super().stop()

    def _add_audio(
        self, name: str, *, data: np.ndarray, sample_rate: int
    ) -> AudioHandle:
        if self._recorder.active_step is None:
            raise RuntimeError("add_audio() is only valid inside server.at(t).")
        return self._recorder.add_audio(name, data=data, sample_rate=sample_rate)

    def _dispatch_audio_update(self, op: dict[str, Any]) -> None:
        self._recorder.dispatch_audio_update(op)

    def _send_runtime_call(self, method: str, payload: dict[str, Any]) -> None:
        self._websock_server.queue_message(make_runtime_message(method, payload))

    @property
    def _fps(self) -> float:
        return self._controller.fps

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
