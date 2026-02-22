"""Playback controller — owns play/pause/seek and render application state."""

from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Protocol

from .timeline import SceneRenderer, Timeline


class TransportListener(Protocol):
    def on_play(self, step: int, fps: float) -> None: ...
    def on_pause(self, step: int) -> None: ...
    def on_seek(self, step: int, fps: float) -> None: ...
    def on_fps_change(self, fps: float, step: int) -> None: ...


class PlaybackController:
    def __init__(
        self,
        num_steps: int,
        fps: float,
        event_loop: asyncio.AbstractEventLoop,
        timeline: Timeline,
        renderer: SceneRenderer,
        on_render_complete: Callable[[int, list[threading.Event]], None],
    ) -> None:
        self._num_steps = num_steps
        self._current_time = 0
        self._fps = fps if fps > 0 else 1.0
        self._event_loop = event_loop
        self._timeline = timeline
        self._renderer = renderer
        self._on_render_complete = on_render_complete

        self._playback_task: asyncio.Task[None] | None = None
        self._listeners: list[TransportListener] = []

        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="viser4d-render",
        )
        self._applied_time: int | None = None
        self._render_in_flight = False

        self._pending_target: int | None = None
        self._pending_interrupt_playback = False
        self._pending_notify_seek = False
        self._pending_done_events: list[threading.Event] = []

    @property
    def current_time(self) -> int:
        return self._current_time

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def num_steps(self) -> int:
        return self._num_steps

    def set_current_time(self, t: int) -> None:
        self._current_time = t

    def is_playing(self) -> bool:
        return self._playback_task is not None and not self._playback_task.done()

    def add_listener(self, listener: TransportListener) -> None:
        self._listeners.append(listener)

    def seek(self, t: int, blocking: bool = False) -> None:
        assert 0 <= t < self._num_steps
        if blocking:
            done = threading.Event()
            self._event_loop.call_soon_threadsafe(
                self._queue_render_request_on_loop,
                t,
                done,
                True,
                True,
            )
            done.wait()
        else:
            self._event_loop.call_soon_threadsafe(
                self._queue_render_request_on_loop,
                t,
                None,
                True,
                True,
            )

    def request_render(self, t: int) -> None:
        """Schedule a render without seek transport side effects."""
        assert 0 <= t < self._num_steps
        self._event_loop.call_soon_threadsafe(
            self._queue_render_request_on_loop,
            t,
            None,
            False,
            False,
        )

    def invalidate_applied_state(self) -> None:
        self._event_loop.call_soon_threadsafe(self._invalidate_applied_state_on_loop)

    def play(self, fps: float, loop: bool = True) -> None:
        self.set_fps(fps)
        self._event_loop.call_soon_threadsafe(self._start_playback, loop)

    def pause(self) -> None:
        self._event_loop.call_soon_threadsafe(self._pause_on_loop)

    def set_fps(self, fps: float) -> None:
        self._fps = fps if fps > 0 else 1.0
        for listener in self._listeners:
            listener.on_fps_change(self._fps, self._current_time)

    def shutdown(self) -> None:
        try:
            self._event_loop.call_soon_threadsafe(self._stop_playback_task)
        except RuntimeError:
            # Server loop may already be closed during shutdown.
            self._stop_playback_task()
        self._executor.shutdown(wait=False, cancel_futures=True)

    def _invalidate_applied_state_on_loop(self) -> None:
        self._renderer.reset()
        self._applied_time = None

    def _queue_render_request_on_loop(
        self,
        t: int,
        done: threading.Event | None,
        interrupt_playback: bool,
        notify_seek: bool,
    ) -> None:
        if done is not None:
            self._pending_done_events.append(done)
        self._pending_target = t
        self._pending_interrupt_playback = (
            self._pending_interrupt_playback or interrupt_playback
        )
        self._pending_notify_seek = self._pending_notify_seek or notify_seek
        self._pump_render_on_loop()

    def _pump_render_on_loop(self) -> None:
        if self._render_in_flight:
            return

        target = self._pending_target
        if target is None:
            return

        done_events = list(self._pending_done_events)
        self._pending_done_events.clear()

        interrupt_playback = self._pending_interrupt_playback
        notify_seek = self._pending_notify_seek
        self._pending_target = None
        self._pending_interrupt_playback = False
        self._pending_notify_seek = False

        if interrupt_playback:
            self._stop_playback_task()

        if notify_seek:
            for listener in self._listeners:
                listener.on_seek(target, self._fps)

        self._render_in_flight = True
        from_time = self._applied_time
        future = self._executor.submit(self._timeline.diff_between, from_time, target)
        future.add_done_callback(
            lambda fut, tt=target, evs=done_events: self._event_loop.call_soon_threadsafe(
                self._on_diff_ready, tt, fut.result(), evs
            )
        )

    def _on_diff_ready(
        self,
        target_time: int,
        diff: Any,
        done_events: list[threading.Event],
    ) -> None:
        self._render_in_flight = False
        self._renderer.apply_diff(diff)
        self._applied_time = target_time
        self._current_time = target_time
        self._on_render_complete(target_time, done_events)
        self._pump_render_on_loop()

    def _start_playback(self, loop: bool) -> None:
        if not self.is_playing():
            self._playback_task = asyncio.create_task(self._playback_loop(loop))
        for listener in self._listeners:
            listener.on_play(self._current_time, self._fps)

    def _pause_on_loop(self) -> None:
        self._stop_playback_task()
        for listener in self._listeners:
            listener.on_pause(self._current_time)

    def _stop_playback_task(self) -> None:
        if self._playback_task is not None and not self._playback_task.done():
            self._playback_task.cancel()
        self._playback_task = None

    async def _playback_loop(self, loop: bool) -> None:
        index = self._current_time
        frame_duration = 1.0 / self._fps
        next_frame_time = time.monotonic()

        while True:
            new_duration = 1.0 / self._fps
            if frame_duration != new_duration:
                frame_duration = new_duration
                next_frame_time = time.monotonic()

            now = time.monotonic()
            if now > next_frame_time + frame_duration:
                frames_behind = int((now - next_frame_time) / frame_duration)
                index = min(index + frames_behind, self._num_steps - 1)
                next_frame_time += frames_behind * frame_duration

            self._queue_render_request_on_loop(index, None, False, False)

            index += 1
            next_frame_time += frame_duration

            if index >= self._num_steps:
                if not loop:
                    break
                index = 0
                next_frame_time = time.monotonic()
                for listener in self._listeners:
                    listener.on_play(0, self._fps)
                continue

            delay = next_frame_time - time.monotonic()
            await asyncio.sleep(delay if delay > 0 else 0)

        for listener in self._listeners:
            listener.on_pause(self._current_time)
        if self._playback_task is asyncio.current_task():
            self._playback_task = None
