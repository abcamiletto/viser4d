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
import threading
import time
from bisect import bisect_right
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator

import numpy as np
import viser as _viser

from .audio import AudioApi, AudioHandle
from .gui import PlaybackControls
from .op import CompressionMode, Op, OpKind

if TYPE_CHECKING:
    from viser import SceneApi


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
            Defaults to 1MB.
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

    _DEFAULT_LAZY_THRESHOLD_BYTES = 1024 * 1024  # 1MB
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
        self._lazy_threshold_bytes = (
            lazy_threshold_bytes or self._DEFAULT_LAZY_THRESHOLD_BYTES
        )
        self._compression = compression or self._DEFAULT_COMPRESSION
        self._playback_task: asyncio.Task[None] | None = None
        self._current_time = 0
        self._fps = fps if fps > 0 else 1.0
        self._audio_timeline_fps = self._fps
        self._timestep_callbacks: list[Callable[[int], None]] = []

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

        Example:
            >>> with server.at(5):
            ...     handle = server.scene.add_frame("/frame")
            ...     handle.position = (1.0, 2.0, 3.0)
        """
        assert 0 <= t < self.num_steps
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
        event_loop = self.get_event_loop()
        if self._is_on_server_loop_thread(event_loop):
            self._start_playback(loop)
            return
        event_loop.call_soon_threadsafe(self._start_playback, loop)

    def pause(self) -> None:
        """Pause playback.

        Stops playback and leaves the scene at the current timestep.
        Safe to call even if playback is not running. This is non-blocking when
        called from outside the server event loop.
        """
        event_loop = self.get_event_loop()
        if self._is_on_server_loop_thread(event_loop):
            self._pause_playback_on_loop()
            return
        event_loop.call_soon_threadsafe(self._pause_playback_on_loop)

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
        self.pause()
        self._render_timestep(t)
        self._audio_api.on_seek(t, self._fps)

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

    @property
    def handles(self) -> list[str]:
        """Names of all recorded scene handles.

        Returns a list of all handle names that have been recorded in the
        timeline. Useful for bulk operations like toggling visibility.

        Returns:
            List of handle name strings.

        Example:
            >>> for name in server.handles:
            ...     if name.startswith("/skeleton/"):
            ...         server.get_handle(name).visible = False
        """
        return self._timeline.handle_names

    def get_handle(self, name: str) -> ProxyHandle:
        """Get a handle by name for manipulation.

        Returns a handle that can be used to modify scene objects. When used
        inside an ``at(t)`` context, changes are recorded to the timeline.
        When used outside, changes are applied immediately to the live scene.

        Args:
            name: The name of the scene object (e.g., "/skeleton/joints").

        Returns:
            A ProxyHandle for the named object.

        Example:
            >>> # Runtime visibility toggle
            >>> handle = server.get_handle("/skeleton/joints")
            >>> handle.visible = False  # Immediate effect
        """
        return ProxyHandle(self._proxy_scene, name)

    def _fire_timestep_callbacks(self, t: int) -> None:
        """Invoke all registered timestep callbacks."""
        for callback in self._timestep_callbacks:
            callback(t)

    def _is_playing(self) -> bool:
        return self._playback_task is not None and not self._playback_task.done()

    def _start_playback(self, loop: bool) -> None:
        if self._is_playing():
            if self._playback_controls is not None:
                self._playback_controls.set_playing(True)
            return
        self._playback_task = asyncio.create_task(self._playback_loop(loop))
        self._on_playback_start(self._current_time, self._fps)

    def _pause_playback_on_loop(self) -> None:
        if self._playback_task is not None and not self._playback_task.done():
            self._playback_task.cancel()
        self._playback_task = None
        self._on_playback_stop()

    def _is_on_server_loop_thread(self, loop: asyncio.AbstractEventLoop) -> bool:
        loop_thread_id = getattr(loop, "_thread_id", None)
        return loop_thread_id is not None and threading.get_ident() == loop_thread_id

    def _render_timestep(self, t: int) -> None:
        loop = self.get_event_loop()
        if self._is_on_server_loop_thread(loop):
            self._renderer.apply(t)
            self._current_time = t
            self._fire_timestep_callbacks(t)
            return

        future = asyncio.run_coroutine_threadsafe(self._render_timestep_async(t), loop)
        future.result()

    async def _render_timestep_async(self, t: int) -> None:
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
            self._playback_step(index)

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

    def _playback_step(self, t: int) -> None:
        """Apply one playback frame."""
        self._render_timestep(t)

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


# =============================================================================
# Recording proxies
# =============================================================================


class ProxyScene:
    """Context-aware scene proxy.

    Inside an ``at(t)`` context, operations are recorded to the timeline.
    Outside, operations are forwarded to the live viser scene.
    """

    def __init__(
        self,
        server: Viser4dServer,
        timeline: Timeline,
        live_scene: SceneApi,
        lazy_threshold_bytes: int,
        compression: CompressionMode,
    ) -> None:
        self._server = server
        self._timeline = timeline
        self._live_scene = live_scene
        self._lazy_threshold_bytes = lazy_threshold_bytes
        self._compression = compression
        self._recording_context = threading.local()

    def add_audio(
        self, name: str, *, data: np.ndarray, sample_rate: int
    ) -> AudioHandle:
        """Add an audio track starting at the current recording timestep.

        Must be called inside an ``at(t)`` context. The audio will begin
        playing at timestep *t* during playback.

        Returns an :class:`AudioHandle` whose properties (e.g. ``volume``)
        sync to the client, matching viser's handle pattern.

        Args:
            name: Identifier for this audio track (e.g. ``"/narration"``).
            data: Audio samples (``int16`` or ``float32``). 1-D mono or
                2-D ``(N, channels)`` stereo.
            sample_rate: Sample rate in Hz (e.g. 44100).

        Returns:
            An AudioHandle for the new track.

        Raises:
            RuntimeError: If called outside an ``at(t)`` context.
        """
        if self._recording_time is None:
            raise RuntimeError("add_audio() must be called inside an at(t) context.")
        return self._server._audio_api.add_track(
            name,
            data,
            sample_rate,
            start_step=self._recording_time,
        )

    def __getattr__(self, name: str) -> Any:
        # Outside recording context - forward to live scene
        if self._recording_time is None:
            return getattr(self._live_scene, name)

        # Inside recording context - record operations
        if name.startswith("add_"):

            def _add(*args: Any, **kwargs: Any) -> ProxyHandle:
                target = self._target_from_add(args, kwargs)
                op = Op.create(
                    kind=OpKind.ADD,
                    target=target,
                    member=name,
                    args=args,
                    kwargs=kwargs,
                    threshold_bytes=self._lazy_threshold_bytes,
                    compression=self._compression,
                )
                self._record(op)
                return ProxyHandle(self, target)

            return _add

        if name == "remove_by_name":

            def _remove(target: str) -> None:
                self._record(
                    Op.create(
                        kind=OpKind.REMOVE,
                        target=target,
                        member=name,
                        threshold_bytes=self._lazy_threshold_bytes,
                        compression=self._compression,
                    )
                )

            return _remove

        # For any other attribute, forward to the live scene even while recording.
        return getattr(self._live_scene, name)

    def _set_time(self, time_step: int | None) -> None:
        self._recording_time = time_step

    @property
    def _recording_time(self) -> int | None:
        return getattr(self._recording_context, "time_step", None)

    @_recording_time.setter
    def _recording_time(self, value: int | None) -> None:
        self._recording_context.time_step = value

    def _record(self, op: Op) -> None:
        if self._recording_time is None:
            raise RuntimeError("Cannot record operation when time is not set.")
        t = self._recording_time
        if op.kind is OpKind.ADD:
            self._timeline.record_add(t, op.target, op)
        elif op.kind is OpKind.REMOVE:
            self._timeline.record_remove(t, op.target)
        else:
            self._timeline.record_set(t, op.target, op.member, op.args[0])

    @staticmethod
    def _target_from_add(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        return kwargs.get("name", args[0])


class ProxyHandle:
    """Handle proxy that records or forwards attribute access.

    When accessed inside an ``at(t)`` context, operations are recorded to the
    timeline. When accessed outside, operations are forwarded to the live
    viser handle.
    """

    __slots__ = ("_parent_scene", "_name")

    def __init__(self, parent_scene: ProxyScene, name: str) -> None:
        self._parent_scene = parent_scene
        self._name = name

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return

        if self._parent_scene._recording_time is not None:
            # Inside at() context - record to timeline
            op = Op.create(
                kind=OpKind.SET,
                target=self._name,
                member=name,
                args=(value,),
                threshold_bytes=self._parent_scene._lazy_threshold_bytes,
                compression=self._parent_scene._compression,
            )
            self._parent_scene._record(op)
        else:
            # Outside at() context - forward to live handle
            setattr(self._get_live_handle(), name, value)

    def __getattr__(self, name: str) -> Any:
        # Forward attribute reads to live handle
        return getattr(self._get_live_handle(), name)

    def remove(self) -> None:
        if self._parent_scene._recording_time is not None:
            # Inside at() context - record to timeline
            op = Op.create(
                kind=OpKind.REMOVE,
                target=self._name,
                member="remove",
                threshold_bytes=self._parent_scene._lazy_threshold_bytes,
                compression=self._parent_scene._compression,
            )
            self._parent_scene._record(op)
        else:
            # Outside at() context - forward to live handle
            self._get_live_handle().remove()

    def _get_live_handle(self) -> Any:
        """Get the live viser handle, raising if not yet in live scene."""
        handle = self._parent_scene._live_scene._handle_from_node_name.get(self._name)
        if handle is None:
            raise RuntimeError(
                f"Handle '{self._name}' not in live scene. "
                "Make sure to call seek() after recording."
            )
        return handle


# =============================================================================
# Timeline and rendering
# =============================================================================


class Timeline:
    """Stores temporal operation data."""

    def __init__(self) -> None:
        self._adds: dict[str, _TimeSeries] = {}
        self._removes: dict[str, _TimeSeries] = {}
        self._sets: dict[str, dict[str, _TimeSeries]] = {}

    def record_add(self, t: int, target: str, op: Op) -> None:
        series = self._adds.setdefault(target, _TimeSeries())
        series.add(t, op)

    def record_remove(self, t: int, target: str) -> None:
        series = self._removes.setdefault(target, _TimeSeries())
        series.add(t, None)

    def record_set(self, t: int, target: str, member: str, value: Any) -> None:
        target_sets = self._sets.setdefault(target, {})
        series = target_sets.setdefault(member, _TimeSeries())
        series.add(t, value)

    @property
    def targets(self) -> set[str]:
        return set(self._adds)

    @property
    def handle_names(self) -> list[str]:
        """Names of all handles that have been added."""
        return list(self._adds.keys())

    def get_sets_for(self, target: str) -> dict[str, _TimeSeries]:
        return self._sets.get(target, {})

    def get_add_at(self, target: str, t: int) -> tuple[int, Op, int] | None:
        """Return (add_index, op, add_time) if target should exist at time t."""
        series = self._adds.get(target)
        if series is None:
            return None

        add_index = series.latest_index(t)
        if add_index is None:
            return None

        add_time = series.times[add_index]

        remove_series = self._removes.get(target)
        if remove_series is not None:
            remove_index = remove_series.latest_index(t)
            if remove_index is not None:
                if remove_series.times[remove_index] >= add_time:
                    return None

        return (add_index, series.values[add_index], add_time)


class SceneRenderer:
    """Applies timeline state to a live scene."""

    def __init__(self, timeline: Timeline, scene: SceneApi) -> None:
        self._timeline = timeline
        self._scene = scene
        self._handles: dict[str, Any] = {}
        self._rendered_time: int = -1
        self._rendered_add: dict[str, int] = {}
        self._rendered_set: dict[str, dict[str, int]] = {}

    def apply(self, t: int) -> None:
        if t < self._rendered_time:
            self.reset()
        self._apply_state(t)
        self._rendered_time = t

    def reset(self) -> None:
        """Clear all rendered state."""
        for target in list(self._handles):
            self._remove_handle(target)
        self._rendered_time = -1

    def _remove_handle(self, target: str) -> None:
        self._scene.remove_by_name(target)
        self._handles.pop(target, None)
        self._rendered_add.pop(target, None)
        self._rendered_set.pop(target, None)

    def _apply_state(self, t: int) -> None:
        for target in self._timeline.targets | set(self._handles):
            add_info = self._timeline.get_add_at(target, t)

            if add_info is None:
                if target in self._handles:
                    self._remove_handle(target)
                continue

            add_index, op, add_time = add_info

            if self._rendered_add.get(target) != add_index:
                if target in self._handles:
                    self._remove_handle(target)
                self._handles[target] = getattr(self._scene, op.member)(
                    *op.args, **op.kwargs
                )
                self._rendered_add[target] = add_index
                self._rendered_set[target] = {}

            handle = self._handles[target]
            for member, member_series in self._timeline.get_sets_for(target).items():
                set_index = member_series.latest_index(t)
                if set_index is None:
                    continue
                if member_series.times[set_index] < add_time:
                    continue
                if self._rendered_set[target].get(member) != set_index:
                    setattr(handle, member, member_series.values[set_index])
                    self._rendered_set[target][member] = set_index


# =============================================================================
# Internal data structures
# =============================================================================


class _TimeSeries:
    def __init__(self) -> None:
        self.times: list[int] = []
        self.values: list[Any] = []

    def add(self, t: int, value: Any) -> None:
        index = bisect_right(self.times, t)
        self.times.insert(index, t)
        self.values.insert(index, value)

    def latest_index(self, t: int) -> int | None:
        index = bisect_right(self.times, t)
        if index == 0:
            return None
        return index - 1
