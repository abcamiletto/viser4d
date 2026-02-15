"""Viser server with timeline recording and playback.

Architecture
------------
Context-aware proxies: ProxyScene and ProxyHandle behave differently based on
whether you're inside an ``at(t)`` context or not.

::

    Inside at(t):                          Outside at(t):
    ─────────────                          ──────────────
    server.scene.add_frame(...)            server.scene.add_frame(...)
           │                                      │
           ▼                                      ▼
    ┌─────────────┐                        ┌─────────────┐
    │ ProxyScene  │ ──records──▶ Timeline  │ ProxyScene  │ ──forwards──▶ Live Scene
    └─────────────┘                        └─────────────┘

    handle.position = ...                  handle.position = ...
           │                                      │
           ▼                                      ▼
    ┌─────────────┐                        ┌─────────────┐
    │ ProxyHandle │ ──records──▶ Timeline  │ ProxyHandle │ ──forwards──▶ Live Handle
    └─────────────┘                        └─────────────┘

Playback: SceneRenderer reads from Timeline and applies state to the live scene.
"""

from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator

import viser as _viser

from .audio import AudioApi
from .gui import PlaybackControls
from .op import CompressionMode
from .proxy import ProxyScene
from .timeline import SceneRenderer, Timeline


# =============================================================================
# Public API
# =============================================================================


class Viser4dServer(_viser.ViserServer):
    """Timeline-aware wrapper around :class:`viser.ViserServer`.

    ``Viser4dServer`` records scene operations at integer timesteps and can
    seek or play that recorded timeline onto the live viser scene.

    Design:
        - Inside ``at(t)``, operations are recorded to the timeline.
        - Outside ``at(t)``, operations are forwarded directly to live scene state.
        - During playback/seek, recorded state is rendered for the selected timestep.
        - Audio transport uses the server FPS as its baseline time mapping.

    Args:
        num_steps: Total number of timesteps in the timeline.
        host: Host address to bind the underlying viser server to.
        port: Port for the underlying viser server. Use ``0`` for auto selection.
        label: Optional label displayed in the GUI panel.
        verbose: Whether to print server startup information.
        fps: Initial playback FPS and fixed audio baseline FPS.
        lazy_threshold_bytes: Payloads larger than this are disk-backed.
            ``None`` disables disk-backed payloads (default).
        compression: Compression mode for disk-backed payloads.
            Defaults to :attr:`CompressionMode.FAST`.
        enable_playback_gui: Whether to add built-in playback controls.
        **kwargs: Additional keyword arguments forwarded to ``viser.ViserServer``.

    Example:
        >>> server = Viser4dServer(num_steps=100, fps=30)
        >>> with server.at(0):
        ...     handle = server.scene.add_frame("/frame")
        ...     handle.position = (1.0, 0.0, 0.0)
        >>> server.play(fps=30, loop=True)
    """

    _DEFAULT_COMPRESSION = CompressionMode.FAST
    scene: Any

    def __init__(
        self,
        num_steps: int,
        host: str = "0.0.0.0",
        port: int = 8080,
        label: str | None = None,
        verbose: bool = True,
        fps: float = 30.0,
        lazy_threshold_bytes: int | None = None,
        compression: CompressionMode | None = None,
        enable_playback_gui: bool = True,
        **kwargs: Any,
    ) -> None:
        self.num_steps = num_steps
        self._lazy_threshold_bytes = lazy_threshold_bytes
        self._compression = compression or self._DEFAULT_COMPRESSION
        self._playback_task: asyncio.Task[None] | None = None
        self._current_time = 0
        self._fps = fps if fps > 0 else 1.0
        self._timestep_callbacks: list[Callable[[int], None]] = []
        self._queued_seek: int | None = None
        self._seek_flush_scheduled = False
        self._applied_time: int | None = None
        self._pending_render_time: int | None = None
        self._render_in_flight = False
        self._render_target_time: int | None = None
        self._render_ops: deque[tuple[str, str, Any]] = deque()
        # Run user callbacks off the event loop to keep timeline updates responsive.
        self._callback_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="viser4d-callbacks",
        )
        # Build render diffs off the event loop.
        self._render_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="viser4d-render",
        )

        # Initialize viser server first (creates the live SceneApi on self.scene).
        super().__init__(
            host=host,
            port=port,
            label=label,
            verbose=verbose,
            **kwargs,
        )
        self._live_scene = self.scene

        # Now create components that need _live_scene
        self._audio_api = AudioApi(self, timeline_fps=self._fps)
        self._timeline = Timeline()
        self._proxy_scene = ProxyScene(
            server=self,
            timeline=self._timeline,
            live_scene=self._live_scene,
            lazy_threshold_bytes=self._lazy_threshold_bytes,
            compression=self._compression,
        )
        self._renderer = SceneRenderer(self._timeline, self._live_scene)
        # Expose context-aware scene operations to callers after base init is complete.
        setattr(self, "scene", self._proxy_scene)
        if enable_playback_gui:
            self._playback_controls: PlaybackControls | None = PlaybackControls(self)
        else:
            self._playback_controls = None

    @property
    def current_time(self) -> int:
        """The current timestep in the timeline.

        Returns:
            The timestep index (0 to num_steps - 1).
        """
        return self._current_time

    @contextmanager
    def at(self, t: int) -> Iterator[None]:
        """Context manager for recording operations at a specific timestep.

        All scene operations performed within this context are recorded to the
        timeline at timestep ``t`` rather than being applied immediately.

        Args:
            t: The timestep index (0 to num_steps - 1).

        Yields:
            None

        Raises:
            AssertionError: If t is out of bounds.
            RuntimeError: If called while already inside ``at()``.

        Example:
            >>> with server.at(5):
            ...     handle = server.scene.add_frame("/frame")
            ...     handle.position = (1.0, 2.0, 3.0)
        """
        assert 0 <= t < self.num_steps
        if self._proxy_scene._recording_time is not None:
            raise RuntimeError("Nested at() contexts are not supported.")
        self._proxy_scene._set_time(t)
        try:
            yield
        finally:
            self._proxy_scene._set_time(None)
            if self._applied_time == t:

                def _refresh() -> None:
                    self._renderer.reset()
                    self._applied_time = None
                    self._set_timestep_on_loop(t)

                self.get_event_loop().call_soon_threadsafe(_refresh)

    def play(self, fps: float, loop: bool = True) -> None:
        """Start playback of the timeline.

        Begins advancing through timesteps at the specified frame rate. If
        playback is already running, this method does nothing.

        Args:
            fps: Frames per second for playback speed.
            loop: Whether to loop back to the beginning after reaching the end.

        Example:
            >>> server.play(fps=30, loop=True)
        """
        self._set_fps(fps)
        self.get_event_loop().call_soon_threadsafe(self._start_playback, loop)

    def pause(self) -> None:
        """Pause playback.

        Stops playback and leaves the scene at the current timestep.
        Safe to call even if playback is not running. This is non-blocking when
        called from outside the server event loop.
        """
        self.get_event_loop().call_soon_threadsafe(self._pause_playback_on_loop)

    def seek(self, t: int) -> None:
        """Jump to a specific timestep.

        Non-blocking: stores the latest requested timestep and schedules a
        single loop flush. Rapid repeated calls are coalesced to the latest.

        The actual seek pauses any active playback, renders the scene state at
        timestep ``t``, and then invokes registered timestep callbacks.

        Args:
            t: The timestep index (0 to num_steps - 1).

        Raises:
            AssertionError: If t is out of bounds.

        Example:
            >>> server.seek(50)  # Jump to frame 50
        """
        assert 0 <= t < self.num_steps
        self.get_event_loop().call_soon_threadsafe(self._queue_seek_on_loop, t)

    def _queue_seek_on_loop(self, t: int) -> None:
        self._queued_seek = t
        if self._seek_flush_scheduled:
            return
        self._seek_flush_scheduled = True
        self.get_event_loop().call_soon(self._flush_seek_on_loop)

    def _flush_seek_on_loop(self) -> None:
        self._seek_flush_scheduled = False
        t = self._queued_seek
        self._queued_seek = None
        if t is None:
            return
        self._pause_playback_on_loop(notify_audio=False)
        self._set_timestep_on_loop(t)
        self._audio_api.on_seek(t, self._fps)

    def on_timestep_change(self, callback: Callable[[int], None]) -> None:
        """Register a callback to be invoked when the timestep changes.

        Callbacks are fired after viser4d applies its own recorded state,
        allowing them to layer additional visualizations on top. This is useful
        when you want to use viser4d's timeline infrastructure but manage your
        own visualization logic. Callbacks run on a dedicated worker thread,
        not on viser's event loop thread.

        Args:
            callback: A function that takes the new timestep as its only argument.

        Example:
            >>> def on_timestep(t: int) -> None:
            ...     update_video_frames(t)
            ...     update_body_meshes(t)
            >>> server.on_timestep_change(on_timestep)
        """
        self._timestep_callbacks.append(callback)

    def _fire_timestep_callbacks(self, t: int) -> None:
        """Invoke all registered timestep callbacks."""
        for callback in tuple(self._timestep_callbacks):
            self._callback_executor.submit(callback, t)

    def stop(self) -> None:
        """Stop the server and release worker threads."""
        try:
            super().stop()
        finally:
            self._render_executor.shutdown(wait=False, cancel_futures=True)
            self._callback_executor.shutdown(wait=False, cancel_futures=True)

    def _is_playing(self) -> bool:
        return self._playback_task is not None and not self._playback_task.done()

    def _start_playback(self, loop: bool) -> None:
        if self._is_playing():
            if self._playback_controls is not None:
                self._playback_controls.set_playing(True)
            return
        self._playback_task = asyncio.create_task(self._playback_loop(loop))
        self._audio_api.on_play(self._current_time, self._fps)
        if self._playback_controls is not None:
            self._playback_controls.set_playing(True)

    def _pause_playback_on_loop(self, notify_audio: bool = True) -> None:
        if self._playback_task is not None and not self._playback_task.done():
            self._playback_task.cancel()
        self._playback_task = None
        if notify_audio:
            self._audio_api.on_pause()
        if self._playback_controls is not None:
            self._playback_controls.set_playing(False)

    def _set_timestep_on_loop(self, t: int) -> None:
        self._pending_render_time = t
        self._pump_render_on_loop()

    def _pump_render_on_loop(self) -> None:
        if self._render_ops:
            kind, target, payload = self._render_ops.popleft()
            if kind == "remove":
                self._renderer.remove_node(target)
            elif kind == "create_or_replace":
                self._renderer.create_or_replace_node(target, payload)
            else:
                self._renderer.update_node_members(target, payload)
            self.get_event_loop().call_soon(self._pump_render_on_loop)
            return

        target_time = self._render_target_time
        if target_time is not None:
            self._render_target_time = None
            self._applied_time = target_time
            self._current_time = target_time
            self._fire_timestep_callbacks(target_time)

        if self._render_in_flight:
            return

        target_time = self._pending_render_time
        if target_time is None:
            return

        self._pending_render_time = None
        self._render_in_flight = True
        future = self._render_executor.submit(
            self._timeline.diff_between,
            self._applied_time,
            target_time,
        )

        loop = self.get_event_loop()
        future.add_done_callback(
            lambda fut, target_time=target_time: loop.call_soon_threadsafe(
                self._on_render_diff_ready_on_loop,
                target_time,
                fut.result(),
            )
        )

    def _on_render_diff_ready_on_loop(self, target_time: int, diff: Any) -> None:
        self._render_in_flight = False
        self._render_target_time = target_time
        self._render_ops.extend(
            ("remove", target, None) for target in diff.nodes_to_remove
        )
        self._render_ops.extend(
            ("create_or_replace", target, state)
            for target, state in diff.nodes_to_create_or_replace.items()
        )
        self._render_ops.extend(
            ("update", target, updates)
            for target, updates in diff.member_updates.items()
        )
        self._pump_render_on_loop()

    async def _playback_loop(self, loop: bool) -> None:
        """Main playback loop. Runs on the server event loop."""
        index = self._current_time
        frame_duration = 1.0 / self._fps
        next_frame_time = time.monotonic()

        while True:
            # Handle dynamic FPS changes
            new_duration = 1.0 / self._fps
            if frame_duration != new_duration:
                frame_duration = new_duration
                next_frame_time = time.monotonic()

            # Skip frames if rendering is too slow
            now = time.monotonic()
            if now > next_frame_time + frame_duration:
                frames_behind = int((now - next_frame_time) / frame_duration)
                index = min(index + frames_behind, self.num_steps - 1)
                next_frame_time += frames_behind * frame_duration

            # Render current frame
            self._set_timestep_on_loop(index)

            # Advance to next frame
            index += 1
            next_frame_time += frame_duration

            # Handle end of timeline
            if index >= self.num_steps:
                if not loop:
                    break
                index = 0
                next_frame_time = time.monotonic()
                self._audio_api.on_play(0, self._fps)
                if self._playback_controls is not None:
                    self._playback_controls.set_playing(True)
                continue

            # Wait for next frame
            delay = next_frame_time - time.monotonic()
            await asyncio.sleep(delay if delay > 0 else 0)

        self._audio_api.on_pause()
        if self._playback_controls is not None:
            self._playback_controls.set_playing(False)
        if self._playback_task is asyncio.current_task():
            self._playback_task = None

    def _set_fps(self, fps: float) -> None:
        self._fps = fps if fps > 0 else 1.0
        if self._playback_controls is not None:
            self._playback_controls.set_fps(self._fps)
        if self._is_playing():
            self._audio_api.on_fps_change(self._fps)
