"""Viser server with timeline recording and playback.

Architecture
------------
::

    ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
    │                                              Viser4dServer                                                │
    │                      - Owns the timeline and playback state                                             │
    │                      - Provides the public API (at, play, pause, seek)                                  │
    └─────────────────────────────────────────────────────────────────────────────────────────────────────────┘
                                                        │
                ┌───────────────────┬───────────────────┼───────────────────┬───────────────────┐
                ▼                   ▼                   ▼                   ▼                   ▼
    ┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐ ┌───────────────────┐
    │    ProxyScene     │ │     Timeline      │ │   SceneRenderer   │ │   Scene (live)    │ │ PlaybackControls  │
    │                   │ │                   │ │                   │ │                   │ │                   │
    │ - Records ops     │ │ - Ops by timestep │ │ - Apply ops to    │ │ - Real viser      │ │ - GUI widgets     │
    │   when inside     │ │ - Temporal        │ │   the live scene  │ │   scene           │ │ - Event handlers  │
    │   at(t) context   │ │   storage         │ │ - Track render    │ │                   │ │                   │
    │                   │ │                   │ │   state           │ │                   │ │                   │
    └─────────┬─────────┘ └─────────┬─────────┘ └─────────┬─────────┘ └─────────┬─────────┘ └───────────────────┘
              │                     │                     │                     │
              └─────────────────────┘                     └─────────────────────┘
                    writes to                                   reads from

ProxyScene records operations to the Timeline during ``at(t)`` contexts.
SceneRenderer reads from the Timeline and applies state to the live Scene during playback.
"""

from __future__ import annotations

import threading
import time
from bisect import bisect_right
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Iterator

import viser as _viser

from .gui import PlaybackControls
from .op import CompressionMode, Op, OpKind

if TYPE_CHECKING:
    from viser import SceneApi


# =============================================================================
# Public API
# =============================================================================


class Viser4dServer(_viser.ViserServer):
    """Viser server with timeline recording and playback controls.

    Wraps a standard viser server with the ability to record scene operations
    across discrete timesteps and play them back with seeking support.

    Args:
        num_steps: Total number of timesteps in the timeline.
        host: Host address to bind the server to.
        port: Port number for the server. Use 0 for automatic assignment.
        label: Optional label displayed in the viser UI.
        verbose: Whether to print server startup information.
        enable_webxr: Whether to enable WebXR support.
        ssl_context: Optional SSL context for HTTPS.
        lazy_threshold_bytes: Payloads larger than this are stored on disk.
            Defaults to 1MB.
        compression: Compression mode for disk-backed payloads.
            Defaults to CompressionMode.FAST.
        **kwargs: Additional arguments passed to the underlying ViserServer.

    Example:
        >>> server = Viser4dServer(num_steps=100)
        >>> with server.at(0):
        ...     handle = server.scene.add_frame("/frame")
        ...     handle.position = (1.0, 0.0, 0.0)
        >>> server.play(fps=30, loop=True)
    """

    _DEFAULT_FPS = 30.0
    _DEFAULT_LAZY_THRESHOLD_BYTES = 1024 * 1024  # 1MB
    _DEFAULT_COMPRESSION = CompressionMode.FAST

    def __init__(
        self,
        num_steps: int,
        host: str = "0.0.0.0",
        port: int = 8080,
        label: str | None = None,
        verbose: bool = True,
        enable_webxr: bool = False,
        ssl_context: Any = None,
        lazy_threshold_bytes: int | None = None,
        compression: CompressionMode | None = None,
        **kwargs: Any,
    ) -> None:
        self._recording = False
        self.num_steps = num_steps
        self._lazy_threshold_bytes = (
            lazy_threshold_bytes or self._DEFAULT_LAZY_THRESHOLD_BYTES
        )
        self._compression = compression or self._DEFAULT_COMPRESSION
        self._timeline = Timeline()
        self._proxy_scene = ProxyScene(
            self._timeline, self._lazy_threshold_bytes, self._compression
        )
        self._playback_thread: threading.Thread | None = None
        self._playback_stop = threading.Event()
        self._current_time = 0
        self._fps = self._DEFAULT_FPS
        self._timestep_callbacks: list[Callable[[int], None]] = []
        super().__init__(
            host=host,
            port=port,
            label=label,
            verbose=verbose,
            enable_webxr=enable_webxr,
            ssl_context=ssl_context,
            **kwargs,
        )
        self._proxy_scene._set_live_scene(self._live_scene)
        self._renderer = SceneRenderer(self._timeline, self._live_scene)
        self._playback_controls = PlaybackControls(self)

    @property
    def scene(self) -> ProxyScene | SceneApi:
        """The scene API for adding and manipulating 3D objects.

        When accessed inside an ``at(t)`` context, returns a proxy that records
        operations. Outside of recording contexts, returns the live viser scene.

        Returns:
            ProxyScene when recording, SceneApi otherwise.
        """
        return self._proxy_scene if self._recording else self._live_scene

    @scene.setter
    def scene(self, value: SceneApi) -> None:
        self._live_scene = value
        self._renderer = SceneRenderer(self._timeline, value)

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
        self._recording = True
        self._proxy_scene.set_time(t)
        try:
            yield
        finally:
            self._recording = False
            self._proxy_scene.set_time(None)

    def play(self, fps: float, loop: bool = False) -> None:
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
        if self._playback_thread is not None and self._playback_thread.is_alive():
            return

        self._playback_stop = threading.Event()
        self._playback_thread = threading.Thread(
            target=self._playback_loop, args=(loop,), daemon=True
        )
        self._playback_thread.start()

    def pause(self) -> None:
        """Pause playback.

        Stops the playback thread and leaves the scene at the current timestep.
        Safe to call even if playback is not running.
        """
        if self._playback_thread is not None and self._playback_thread.is_alive():
            self._playback_stop.set()
            self._playback_thread.join()

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
        self._current_time = t
        self._renderer.apply(t)
        self._fire_timestep_callbacks(t)

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
        return list(self._timeline._adds.keys())

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

    def _playback_loop(self, loop: bool) -> None:
        """Main playback loop. Runs in a separate thread."""
        index = self._current_time
        frame_duration = 1.0 / self._fps
        next_frame_time = time.monotonic()

        while True:
            if self._playback_stop.is_set():
                return

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
            if not self._playback_step(index):
                return

            # Advance to next frame
            index += 1
            next_frame_time += frame_duration

            # Handle end of timeline
            if index >= self.num_steps:
                if not loop:
                    break
                index = 0
                next_frame_time = time.monotonic()
                continue

            # Wait for next frame
            delay = next_frame_time - time.monotonic()
            if delay > 0 and self._playback_stop.wait(timeout=delay):
                return

        self._playback_controls.set_playing(False)

    def _playback_step(self, t: int) -> bool:
        """Apply frame and return False if stopped."""
        if self._playback_stop.is_set():
            return False
        self._renderer.apply(t)
        self._current_time = t
        self._fire_timestep_callbacks(t)
        return True

    def _set_fps(self, fps: float) -> None:
        self._fps = fps
        self._playback_controls.set_fps(fps)


# =============================================================================
# Recording proxies
# =============================================================================


class ProxyScene:
    """Scene proxy that records operations to a timeline."""

    def __init__(
        self,
        timeline: Timeline,
        lazy_threshold_bytes: int,
        compression: CompressionMode,
    ) -> None:
        self._timeline = timeline
        self._lazy_threshold_bytes = lazy_threshold_bytes
        self._compression = compression
        self._recording_time: int | None = None
        self._live_scene: SceneApi | None = None

    def _set_live_scene(self, scene: SceneApi) -> None:
        self._live_scene = scene

    def __getattr__(self, name: str) -> Any:
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
                self.record(op)
                return ProxyHandle(self, target)

            return _add

        if name == "remove_by_name":

            def _remove(target: str) -> None:
                self.record(
                    Op.create(
                        kind=OpKind.REMOVE,
                        target=target,
                        member=name,
                        threshold_bytes=self._lazy_threshold_bytes,
                        compression=self._compression,
                    )
                )

            return _remove

        raise AttributeError(f"ProxyScene has no attribute '{name}'")

    def set_time(self, time_step: int | None) -> None:
        self._recording_time = time_step

    def record(self, op: Op) -> None:
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
            self._parent_scene.record(op)
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
            self._parent_scene.record(op)
        else:
            # Outside at() context - forward to live handle
            self._get_live_handle().remove()

    def _get_live_handle(self) -> Any:
        """Get the live viser handle, raising if not yet in live scene."""
        if self._parent_scene._live_scene is None:
            raise RuntimeError(
                f"Handle '{self._name}' cannot be accessed: live scene not initialized."
            )
        handles = self._parent_scene._live_scene._handle_from_node_name
        handle = handles.get(self._name)
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
