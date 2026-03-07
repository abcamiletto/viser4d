from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING, Callable

from ._runtime import clamp

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
    def base_fps(self) -> float:
        return self._base_fps

    @property
    def loop(self) -> bool:
        return self._loop

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def current_timestep(self) -> int:
        return self._current_timestep

    def play(self, fps: float, loop: bool = False) -> None:
        with self._lock:
            current_step = self._transport_step()
            self._fps = float(fps)
            self._loop = bool(loop)
            self._is_playing = True
            self._set_anchor(current_step)

    def pause(self) -> None:
        with self._lock:
            self._set_anchor(self._transport_step())
            self._is_playing = False

    def seek(self, t: int) -> None:
        timestep = clamp(int(t), 0, self._server.num_steps - 1)
        with self._lock:
            self._set_anchor(float(timestep))
        self.set_current_timestep(timestep)

    def on_timestep_change(self, callback: Callable[[int], None]) -> None:
        self._callbacks.append(callback)

    def stop(self) -> None:
        self._stop_event.set()
        self._predictor_thread.join()

    def set_fps(self, fps: float) -> None:
        with self._lock:
            current_step = self._transport_step()
            self._fps = float(fps)
            self._set_anchor(current_step)

    def set_current_timestep(self, timestep: int) -> None:
        timestep = clamp(timestep, 0, self._server.num_steps - 1)
        if timestep == self._current_timestep:
            return
        self._current_timestep = timestep
        for callback in list(self._callbacks):
            callback(timestep)

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
            should_sync_buttons = False
            with self._lock:
                is_playing = self._is_playing
                timestep = int(self._transport_step())
                if (
                    is_playing
                    and not self._loop
                    and timestep >= self._server.num_steps - 1
                ):
                    timestep = self._server.num_steps - 1
                    self._set_anchor(float(timestep))
                    self._is_playing = False
                    is_playing = False
                    should_sync_buttons = True
            if not is_playing and not should_sync_buttons:
                continue
            if should_sync_buttons:
                self._server._sync_client_playback_state(
                    timestep=timestep,
                    fps=self._fps,
                    loop=self._loop,
                    is_playing=False,
                )
            self.set_current_timestep(timestep)
