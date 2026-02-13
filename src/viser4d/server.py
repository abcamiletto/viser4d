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
import logging
import threading
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator

import viser as _viser

from .audio import AudioApi
from .gui import PlaybackControls
from .op import CompressionMode
from .proxy import ProxyScene
from .timeline import SceneRenderer, Timeline

if TYPE_CHECKING:
    from viser import SceneApi

_LOG = logging.getLogger(__name__)


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
        self._audio_timeline_fps = self._fps
        self._timestep_callbacks: list[Callable[[int], None]] = []
        self._pending_seek: int | None = None
        self._seek_dispatch_pending = False

        # Initialize viser server first (creates _live_scene)
        super().__init__(
            host=host,
            port=port,
            label=label,
            verbose=verbose,
            **kwargs,
        )

        # Now create components that need _live_scene
        self._audio_api = AudioApi(self)
        self._timeline = Timeline()
        self._proxy_scene = ProxyScene(
            server=self,
            timeline=self._timeline,
            live_scene=self._live_scene,
            lazy_threshold_bytes=self._lazy_threshold_bytes,
            compression=self._compression,
        )
        self._renderer = SceneRenderer(self._timeline, self._live_scene)
        if enable_playback_gui:
            self._playback_controls: PlaybackControls | None = PlaybackControls(self)
        else:
            self._playback_controls = None

    @property
    def scene(self) -> ProxyScene:
        """The scene API for adding and manipulating 3D objects.

        Context-aware: inside ``at(t)`` operations are recorded to the timeline,
        outside they are applied immediately to the live scene.

        Returns:
            ProxyScene (context-aware, always the same object).
        """
        # During __init__, _proxy_scene doesn't exist yet
        # Use __dict__ to avoid triggering __getattr__ recursion
        if "_proxy_scene" not in self.__dict__:
            return self._live_scene  # type: ignore[return-value]
        return self._proxy_scene

    @scene.setter
    def scene(self, value: SceneApi) -> None:
        # Called by ViserServer.__init__ to set the live scene
        self._live_scene = value

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
            RuntimeError: If called while already inside ``at()`` on this thread.

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
        self._dispatch_on_loop(self._start_playback, loop)

    def pause(self) -> None:
        """Pause playback.

        Stops playback and leaves the scene at the current timestep.
        Safe to call even if playback is not running. This is non-blocking when
        called from outside the server event loop.
        """
        self._dispatch_on_loop(self._pause_playback_on_loop)

    def seek(self, t: int) -> None:
        """Jump to a specific timestep.

        Pauses any active playback and renders the scene state at timestep ``t``.
        Registered timestep callbacks are invoked after the scene is updated.

        Args:
            t: The timestep index (0 to num_steps - 1).

        Raises:
            AssertionError: If t is out of bounds.

        Example:
            >>> server.seek(50)  # Jump to frame 50
        """
        assert 0 <= t < self.num_steps
        self._dispatch_on_loop(self._seek_on_loop, t, wait=True)

    def request_seek(self, t: int) -> None:
        """Queue a seek request and coalesce to the most recent timestep."""
        assert 0 <= t < self.num_steps
        self._dispatch_on_loop(self._queue_seek, t)

    def _queue_seek(self, t: int) -> None:
        self._pending_seek = t
        if self._seek_dispatch_pending:
            return
        self._seek_dispatch_pending = True
        self.get_event_loop().call_soon(self._drain_pending_seek)

    def _drain_pending_seek(self) -> None:
        self._seek_dispatch_pending = False
        t = self._pending_seek
        self._pending_seek = None
        if t is None:
            return
        self._seek_on_loop(t)

    def on_timestep_change(self, callback: Callable[[int], None]) -> None:
        """Register a callback to be invoked when the timestep changes.

        Callbacks are fired after viser4d applies its own recorded state,
        allowing them to layer additional visualizations on top. This is useful
        when you want to use viser4d's timeline infrastructure but manage your
        own visualization logic.

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
        for callback in self._timestep_callbacks:
            try:
                callback(t)
            except Exception:
                _LOG.exception("Timestep callback failed at step %s", t)

    def _dispatch_on_loop(
        self, fn: Callable[..., None], *args: Any, wait: bool = False
    ) -> None:
        loop = self.get_event_loop()
        if self._is_on_server_loop_thread(loop):
            fn(*args)
            return
        if not wait:
            loop.call_soon_threadsafe(fn, *args)
            return

        async def _call() -> None:
            fn(*args)

        asyncio.run_coroutine_threadsafe(_call(), loop).result()

    def _is_playing(self) -> bool:
        return self._playback_task is not None and not self._playback_task.done()

    def _start_playback(self, loop: bool) -> None:
        if self._is_playing():
            if self._playback_controls is not None:
                self._playback_controls.set_playing(True)
            return
        self._playback_task = asyncio.create_task(self._playback_loop(loop))
        self._on_playback_start(self._current_time, self._fps)

    def _pause_playback_on_loop(self, notify_audio: bool = True) -> None:
        if self._playback_task is not None and not self._playback_task.done():
            self._playback_task.cancel()
        self._playback_task = None
        if notify_audio:
            self._audio_api.on_pause()
        if self._playback_controls is not None:
            self._playback_controls.set_playing(False)

    def _seek_on_loop(self, t: int) -> None:
        self._pause_playback_on_loop(notify_audio=False)
        self._render_timestep(t)
        self._audio_api.on_seek(t, self._fps)

    def _is_on_server_loop_thread(self, loop: asyncio.AbstractEventLoop) -> bool:
        loop_thread_id = getattr(loop, "_thread_id", None)
        return loop_thread_id is not None and threading.get_ident() == loop_thread_id

    def _render_timestep(self, t: int) -> None:
        self._dispatch_on_loop(self._render_timestep_on_loop, t, wait=True)

    def _render_timestep_on_loop(self, t: int) -> None:
        self._renderer.apply(t)
        self._current_time = t
        self._fire_timestep_callbacks(t)

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
            self._render_timestep(index)

            # Advance to next frame
            index += 1
            next_frame_time += frame_duration

            # Handle end of timeline
            if index >= self.num_steps:
                if not loop:
                    break
                index = 0
                next_frame_time = time.monotonic()
                self._on_playback_start(0, self._fps)
                continue

            # Wait for next frame
            delay = next_frame_time - time.monotonic()
            await asyncio.sleep(delay if delay > 0 else 0)

        self._on_playback_stop()
        if self._playback_task is asyncio.current_task():
            self._playback_task = None

    def _set_fps(self, fps: float) -> None:
        self._fps = fps if fps > 0 else 1.0
        if self._playback_controls is not None:
            self._playback_controls.set_fps(self._fps)
        if self._is_playing():
            self._audio_api.on_fps_change(self._fps)

    def _on_playback_start(self, step: int, fps: float) -> None:
        """Notify all playback components that playback has started."""
        self._audio_api.on_play(step, fps)
        if self._playback_controls is not None:
            self._playback_controls.set_playing(True)

    def _on_playback_stop(self) -> None:
        """Notify all playback components that playback has stopped."""
        self._audio_api.on_pause()
        if self._playback_controls is not None:
            self._playback_controls.set_playing(False)
