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

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, cast

import viser as _viser

from .audio import AudioApi
from .gui import PlaybackControls
from .op import CompressionMode
from .playback import PlaybackController, TransportListener
from .proxy import ProxyScene
from .timeline import SceneRenderer, Timeline


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
        callback_workers: Number of worker threads for timestep callbacks.
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
        callback_workers: int = 64,
        **kwargs: Any,
    ) -> None:
        self.num_steps = num_steps
        self._lazy_threshold_bytes = lazy_threshold_bytes
        self._compression = compression or self._DEFAULT_COMPRESSION

        # Initialize viser server (creates event loop + live SceneApi).
        super().__init__(
            host=host,
            port=port,
            label=label,
            verbose=verbose,
            **kwargs,
        )
        self._live_scene = self.scene
        event_loop = self.get_event_loop()

        # Core components
        self._timestep_callbacks: list[Callable[[int], None]] = []
        self._callback_executor = ThreadPoolExecutor(
            max_workers=max(1, callback_workers),
            thread_name_prefix="viser4d-callbacks",
        )
        self._timeline = Timeline()
        self._renderer = SceneRenderer(self._timeline, self._live_scene)

        def _on_render_complete(
            target_time: int, done_events: list[threading.Event]
        ) -> None:
            futures = [
                self._callback_executor.submit(cb, target_time)
                for cb in tuple(self._timestep_callbacks)
            ]
            if done_events:

                def _signal_when_done() -> None:
                    for f in futures:
                        f.result()
                    for ev in done_events:
                        ev.set()

                self._callback_executor.submit(_signal_when_done)

        self._playback = PlaybackController(
            num_steps=num_steps,
            fps=fps,
            event_loop=event_loop,
            timeline=self._timeline,
            renderer=self._renderer,
            on_render_complete=_on_render_complete,
        )

        # Audio and GUI
        self._audio_api = AudioApi(self, timeline_fps=fps if fps > 0 else 1.0)
        self._playback.add_listener(cast(TransportListener, self._audio_api))
        self._timestep_callbacks.append(self._audio_api._on_timestep)

        self._proxy_scene = ProxyScene(
            server=self,
            timeline=self._timeline,
            live_scene=self._live_scene,
            lazy_threshold_bytes=self._lazy_threshold_bytes,
            compression=self._compression,
        )
        setattr(self, "scene", self._proxy_scene)

        if enable_playback_gui:
            self._playback_controls: PlaybackControls | None = PlaybackControls(
                self.gui, self._playback
            )
            self._playback.add_listener(
                cast(TransportListener, self._playback_controls)
            )
            self._timestep_callbacks.append(self._playback_controls._on_timestep)
        else:
            self._playback_controls = None

    @property
    def current_time(self) -> int:
        """The current timestep index (0 to num_steps - 1)."""
        return self._playback.current_time

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
            if self._playback.current_time == t:
                self._playback.invalidate_applied_state()
                self._playback.request_render(t)

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
        self._playback.play(fps, loop)

    def pause(self) -> None:
        """Pause playback.

        Stops playback and leaves the scene at the current timestep.
        Safe to call even if playback is not running. This is non-blocking when
        called from outside the server event loop.
        """
        self._playback.pause()

    def seek(self, t: int, blocking: bool = False) -> None:
        """Jump to a specific timestep.

        By default non-blocking: stores the latest requested timestep and
        schedules a single loop flush. Rapid repeated calls are coalesced to
        the latest.

        The actual seek pauses any active playback, renders the scene state at
        timestep ``t``, and then invokes registered timestep callbacks.

        Args:
            t: The timestep index (0 to num_steps - 1).
            blocking: If ``True``, block until the render and all timestep
                callbacks have completed.

        Raises:
            AssertionError: If t is out of bounds.

        Example:
            >>> server.seek(50)  # Jump to frame 50
            >>> server.seek(50, blocking=True)  # Wait for completion
        """
        self._playback.seek(t, blocking)

    def get_thread_pool(self) -> ThreadPoolExecutor:
        """Return the callback thread pool for offloading work outside the event loop."""
        return self._callback_executor

    def on_timestep_change(self, callback: Callable[[int], None]) -> None:
        """Register a callback to be invoked when the timestep changes.

        Callbacks are fired after viser4d applies its own recorded state,
        allowing them to layer additional visualizations on top. This is useful
        when you want to use viser4d's timeline infrastructure but manage your
        own visualization logic. Callback jobs run on a dedicated thread pool,
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

    def serialize(
        self,
        path: str | Path | None = None,
        *,
        static_scene: bool = False,
        start_timestep: int = 0,
        end_timestep: int | None = None,
        fps: float = 30.0,
    ) -> bytes:
        """Serialize scene data to viser bytes, optionally across timesteps.

        Args:
            path: Optional output file path. If provided, bytes are also written.
            static_scene: If ``True``, only serialize the current scene snapshot.
            start_timestep: First timestep to render when ``static_scene=False``.
            end_timestep: Last timestep to render when ``static_scene=False``.
                Defaults to ``num_steps - 1``.
            fps: Playback FPS used for inserted sleep durations.

        Returns:
            Serialized ``.viser`` bytes.
        """
        serializer = self.get_scene_serializer()

        if not static_scene:
            assert fps > 0
            assert 0 <= start_timestep < self.num_steps
            last_timestep = self.num_steps - 1 if end_timestep is None else end_timestep
            assert start_timestep <= last_timestep < self.num_steps
            for t in range(start_timestep, last_timestep + 1):
                self.seek(t, blocking=True)
                serializer.insert_sleep(1.0 / fps)

        data = serializer.serialize()
        if path is not None:
            Path(path).write_bytes(data)
        return data

    def stop(self) -> None:
        """Stop the server and release worker threads."""
        try:
            super().stop()
        finally:
            self._playback.shutdown()
            self._callback_executor.shutdown(wait=False, cancel_futures=True)
            self._timestep_callbacks.clear()
