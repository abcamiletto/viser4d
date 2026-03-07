from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import viser

from . import _viser_private as impl
from ._runtime import clamp, make_runtime_message, runtime_config_payload

if TYPE_CHECKING:
    from viser._viser import ClientHandle

    from ._server import Viser4dServer


class ClientPlaybackHandle:
    def __init__(self, server: Viser4dServer, client: ClientHandle) -> None:
        self._server = server
        self._client = client
        self._fps = server._fps
        self._loop = server._loop
        self._is_playing = server._is_playing
        self._current_timestep = server._current_timestep
        self._syncing_fps_slider = False
        self._syncing_timestep_slider = False
        self._lock = threading.RLock()

        self._timestep_sync = client.gui.add_number(
            "__viser4d_timestep_sync__",
            self._current_timestep,
            min=0,
            max=max(server.num_steps - 1, 0),
            step=1,
            visible=False,
        )
        with client.gui.add_folder("Playback"):
            self._timeline_slider = client.gui.add_slider(
                "Timestep",
                min=0,
                max=max(server.num_steps - 1, 0),
                step=1,
                initial_value=self._current_timestep,
            )
            self._fps_slider = client.gui.add_slider(
                "FPS",
                min=1.0,
                max=120.0,
                step=1.0,
                initial_value=self._fps,
            )
            self._step_buttons = client.gui.add_button_group("Step", ("Prev", "Next"))
            self._play_button = client.gui.add_button(
                "Play",
                color="green",
                icon=viser.Icon.PLAYER_PLAY_FILLED,
            )
            self._pause_button = client.gui.add_button(
                "Pause",
                color="yellow",
                icon=viser.Icon.PLAYER_PAUSE_FILLED,
                visible=False,
            )

        @self._timestep_sync.on_update
        def _sync(_event: Any) -> None:
            self._sync_from_client(int(self._timestep_sync.value))

        @self._timeline_slider.on_update
        def _sync_slider(_event: Any) -> None:
            with self._lock:
                syncing_timestep_slider = self._syncing_timestep_slider
            if syncing_timestep_slider:
                return
            self.seek(int(self._timeline_slider.value))

        @self._fps_slider.on_update
        def _sync_fps(_event: Any) -> None:
            with self._lock:
                syncing_fps_slider = self._syncing_fps_slider
            if syncing_fps_slider:
                return
            self.set_fps(float(self._fps_slider.value))

        @self._step_buttons.on_click
        def _step(_event: Any) -> None:
            with self._lock:
                current_timestep = self._current_timestep
            if self._step_buttons.value == "Prev":
                self.seek(current_timestep - 1)
            else:
                self.seek(current_timestep + 1)

        @self._play_button.on_click
        def _play(_event: Any) -> None:
            with self._lock:
                fps = self._fps
                loop = self._loop
            self.play(fps, loop=loop)

        @self._pause_button.on_click
        def _pause(_event: Any) -> None:
            self.pause()

        self.sync_runtime_config()
        self._sync_playback_buttons()
        if self._is_playing:
            self.play(self._fps, loop=self._loop)
        elif self._current_timestep != 0:
            self.seek(self._current_timestep)

    @property
    def fps(self) -> float:
        with self._lock:
            return self._fps

    @property
    def loop(self) -> bool:
        with self._lock:
            return self._loop

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._is_playing

    @property
    def current_timestep(self) -> int:
        with self._lock:
            return self._current_timestep

    def play(self, fps: float, loop: bool = False) -> None:
        with self._lock:
            self._fps = float(fps)
            self._loop = bool(loop)
            self._is_playing = True
            payload = {"fps": self._fps, "loop": self._loop}
        self._set_fps_slider_value(payload["fps"])
        self._sync_playback_buttons()
        self._send_runtime_call("play", payload)

    def pause(self) -> None:
        with self._lock:
            self._is_playing = False
        self._sync_playback_buttons()
        self._send_runtime_call("pause", {})

    def seek(self, t: int) -> None:
        timestep = clamp(int(t), 0, self._server.num_steps - 1)
        self._set_current_timestep(timestep)
        self._send_runtime_call("seek", {"step": timestep})

    def set_fps(self, fps: float) -> None:
        with self._lock:
            self._fps = float(fps)
            payload = {"fps": self._fps, "loop": self._loop}
        self._set_fps_slider_value(payload["fps"])
        self._send_runtime_call("setFps", payload)

    def sync_runtime_config(self) -> None:
        with self._lock:
            fps = self._fps
            loop = self._loop
        self._send_runtime_call(
            "configure",
            runtime_config_payload(
                num_steps=self._server.num_steps,
                fps=fps,
                base_fps=self._server._base_fps,
                loop=loop,
                timestep_sync_uuid=impl.gui_uuid(self._timestep_sync),
            ),
        )
        self._timeline_slider.max = max(self._server.num_steps - 1, 0)
        self._timestep_sync.max = max(self._server.num_steps - 1, 0)

    def sync_state(
        self,
        *,
        timestep: int | None = None,
        fps: float | None = None,
        loop: bool | None = None,
        is_playing: bool | None = None,
    ) -> None:
        with self._lock:
            if timestep is not None:
                self._current_timestep = clamp(int(timestep), 0, self._server.num_steps - 1)
            if fps is not None:
                self._fps = float(fps)
            if loop is not None:
                self._loop = bool(loop)
            if is_playing is not None:
                self._is_playing = bool(is_playing)
            current_timestep = self._current_timestep
            current_fps = self._fps
        self._set_current_timestep(current_timestep)
        self._set_fps_slider_value(current_fps)
        self._sync_playback_buttons()

    def _sync_from_client(self, timestep: int) -> None:
        self._set_current_timestep(timestep)
        should_sync_buttons = False
        with self._lock:
            if (
                self._is_playing
                and not self._loop
                and timestep >= self._server.num_steps - 1
            ):
                self._is_playing = False
                should_sync_buttons = True
        if should_sync_buttons:
            self._sync_playback_buttons()

    def _set_current_timestep(self, timestep: int) -> None:
        timestep = clamp(int(timestep), 0, self._server.num_steps - 1)
        with self._lock:
            self._current_timestep = timestep
            self._syncing_timestep_slider = True
        try:
            self._timeline_slider.value = timestep
        finally:
            with self._lock:
                self._syncing_timestep_slider = False

    def _send_runtime_call(self, method: str, payload: dict[str, Any]) -> None:
        self._client._websock_connection.queue_message(
            make_runtime_message(method, payload)
        )

    def _set_fps_slider_value(self, fps: float) -> None:
        if self._fps_slider.value == fps:
            return
        with self._lock:
            self._syncing_fps_slider = True
        try:
            self._fps_slider.value = fps
        finally:
            with self._lock:
                self._syncing_fps_slider = False

    def _sync_playback_buttons(self) -> None:
        with self._lock:
            is_playing = self._is_playing
        self._play_button.visible = not is_playing
        self._pause_button.visible = is_playing
