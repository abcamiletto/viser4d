from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable, Any

from . import _viser_private as impl
from ._runtime import clamp, runtime_config_payload


TIMESTEP_SYNC_SMOOTHING_FACTOR = 0.2
TIMESTEP_SYNC_SNAP_THRESHOLD_SECONDS = 0.1

if TYPE_CHECKING:
    from ._server import Viser4dServer


class TimelineController:
    def __init__(self, server: Viser4dServer, *, fps: float) -> None:
        self._server = server
        self._fps = float(fps)
        self._base_fps = float(fps)
        self._loop = False
        self._is_playing = False
        self._current_timestep = 0
        self._syncing_timestep_slider = False
        self._callbacks: list[Callable[[int], None]] = []
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._anchor_step = 0.0
        self._anchor_time = time.monotonic()
        self._predictor_thread = threading.Thread(
            target=self._predictor_loop,
            name="viser4d-timeline-predictor",
            daemon=True,
        )
        self._predictor_thread.start()

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def loop(self) -> bool:
        return self._loop

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def current_timestep(self) -> int:
        return self._current_timestep

    @property
    def syncing_timestep_slider(self) -> bool:
        return self._syncing_timestep_slider

    def play(self, fps: float, loop: bool = False) -> None:
        with self._lock:
            current_step = self._transport_step()
            self._fps = float(fps)
            self._loop = bool(loop)
            self._is_playing = True
            self._set_anchor(current_step)
        self._sync_playback_buttons()
        self._server._send_runtime_call("play", {"fps": self._fps, "loop": self._loop})

    def pause(self) -> None:
        with self._lock:
            self._set_anchor(self._transport_step())
            self._is_playing = False
        self._sync_playback_buttons()
        self._server._send_runtime_call("pause", {})

    def seek(self, t: int) -> None:
        timestep = clamp(int(t), 0, self._server.num_steps - 1)
        with self._lock:
            self._set_anchor(float(timestep))
        self.set_current_timestep(timestep)
        self._server._send_runtime_call("seek", {"step": timestep})

    def on_timestep_change(self, callback: Callable[[int], None]) -> None:
        self._callbacks.append(callback)

    def stop(self) -> None:
        self._stop_event.set()
        self._predictor_thread.join()

    def sync_from_client(self, timestep: int) -> None:
        timestep = clamp(int(timestep), 0, self._server.num_steps - 1)
        should_sync_buttons = False
        with self._lock:
            if (
                self._is_playing
                and not self._loop
                and timestep >= self._server.num_steps - 1
            ):
                self._set_anchor(float(timestep))
                self._is_playing = False
                should_sync_buttons = True
            elif self._is_playing:
                predicted = self._transport_step()
                error = float(timestep) - predicted
                if self._loop and self._server.num_steps > 0:
                    error = (
                        (error + self._server.num_steps / 2) % self._server.num_steps
                    ) - self._server.num_steps / 2
                error_seconds = abs(error) / self._fps
                correction = (
                    error
                    if error_seconds > TIMESTEP_SYNC_SNAP_THRESHOLD_SECONDS
                    else error * TIMESTEP_SYNC_SMOOTHING_FACTOR
                )
                self._set_anchor(predicted + correction)
            else:
                self._set_anchor(float(timestep))
            is_playing = self._is_playing
        if should_sync_buttons:
            self._sync_playback_buttons()
        if not is_playing:
            self.set_current_timestep(timestep)

    def set_fps(self, fps: float) -> None:
        with self._lock:
            current_step = self._transport_step()
            self._fps = float(fps)
            self._set_anchor(current_step)
            is_playing = self._is_playing
        if is_playing:
            self._server._send_runtime_call(
                "setFps",
                {"fps": self._fps, "loop": self._loop},
            )
            return
        self.sync_runtime_config()

    def sync_runtime_config(self, *, num_steps: int | None = None) -> None:
        self._server._send_runtime_call(
            "configure",
            self.runtime_config_payload(num_steps=num_steps),
        )

    def runtime_config_payload(self, *, num_steps: int | None = None) -> dict[str, Any]:
        return runtime_config_payload(
            num_steps=self._server.num_steps if num_steps is None else num_steps,
            fps=self._fps,
            base_fps=self._base_fps,
            loop=self._loop,
            timestep_sync_uuid=impl.gui_uuid(self._server._timestep_sync),
        )

    def set_current_timestep(self, timestep: int) -> None:
        timestep = clamp(timestep, 0, self._server.num_steps - 1)
        if timestep == self._current_timestep:
            return
        self._current_timestep = timestep
        self._syncing_timestep_slider = True
        try:
            self._server._timeline_slider.value = timestep
        finally:
            self._syncing_timestep_slider = False
        for callback in list(self._callbacks):
            callback(timestep)

    def _sync_playback_buttons(self) -> None:
        self._server._play_button.visible = not self._is_playing
        self._server._pause_button.visible = self._is_playing

    def _set_anchor(self, step: float, *, now: float | None = None) -> None:
        self._anchor_step = max(0.0, min(float(step), self._server.num_steps - 1))
        self._anchor_time = time.monotonic() if now is None else now

    def _transport_step(self, *, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        if not self._is_playing:
            return self._anchor_step
        step = self._anchor_step + (now - self._anchor_time) * self._fps
        if self._loop and self._server.num_steps > 0:
            return step % self._server.num_steps
        return max(0.0, min(step, self._server.num_steps - 1))

    def _predictor_loop(self) -> None:
        while not self._stop_event.wait(0.05):
            with self._lock:
                is_playing = self._is_playing
                timestep = int(self._transport_step())
            if not is_playing:
                continue
            self.set_current_timestep(timestep)
