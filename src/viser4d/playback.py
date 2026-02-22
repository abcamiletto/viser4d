"""Playback controller — owns play/pause/seek state and transport listeners."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Protocol

from .render import RenderPipeline


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
        render_pipeline: RenderPipeline,
    ) -> None:
        self._num_steps = num_steps
        self._current_time = 0
        self._fps = fps if fps > 0 else 1.0
        self._event_loop = event_loop
        self._render_pipeline = render_pipeline
        self._playback_task: asyncio.Task[None] | None = None
        self._queued_seek: int | None = None
        self._seek_flush_scheduled = False
        self._pending_done_events: list[threading.Event] = []
        self._listeners: list[TransportListener] = []

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
                self._queue_seek_on_loop, t, done
            )
            done.wait()
        else:
            self._event_loop.call_soon_threadsafe(self._queue_seek_on_loop, t)

    def play(self, fps: float, loop: bool = True) -> None:
        self.set_fps(fps)
        self._event_loop.call_soon_threadsafe(self._start_playback, loop)

    def pause(self) -> None:
        self._event_loop.call_soon_threadsafe(self._pause_on_loop)

    def set_fps(self, fps: float) -> None:
        self._fps = fps if fps > 0 else 1.0
        for listener in self._listeners:
            listener.on_fps_change(self._fps, self._current_time)

    def _queue_seek_on_loop(
        self, t: int, done: threading.Event | None = None
    ) -> None:
        if done is not None:
            self._pending_done_events.append(done)
        self._queued_seek = t
        if self._seek_flush_scheduled:
            return
        self._seek_flush_scheduled = True
        self._event_loop.call_soon(self._flush_seek_on_loop)

    def _flush_seek_on_loop(self) -> None:
        self._seek_flush_scheduled = False
        t = self._queued_seek
        self._queued_seek = None
        if t is None:
            for ev in self._pending_done_events:
                ev.set()
            self._pending_done_events.clear()
            return
        self._stop_playback_task()
        self._render_pipeline.transfer_done_events(self._pending_done_events)
        self._pending_done_events.clear()
        self._render_pipeline.schedule_render(t)
        for listener in self._listeners:
            listener.on_seek(t, self._fps)

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

            self._render_pipeline.schedule_render(index)

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
