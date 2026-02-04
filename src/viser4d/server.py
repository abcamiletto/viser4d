"""Viser server with timeline recording and playback.

Architecture
------------
::

    ┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
    │                                              ViserServer                                                │
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
from typing import TYPE_CHECKING, Any, Iterator

import viser as _viser

from .gui import PlaybackControls
from .op import Op, OpKind

if TYPE_CHECKING:
    from viser import SceneApi


# =============================================================================
# Public API
# =============================================================================


class ViserServer(_viser.ViserServer):
    """Viser server with timeline recording and playback controls."""

    _DEFAULT_FPS = 30.0
    _DEFAULT_LAZY_THRESHOLD_BYTES = 1024 * 1024  # 1MB

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
        **kwargs: Any,
    ) -> None:
        self._recording = False
        self.num_steps = num_steps
        self._lazy_threshold_bytes = (
            lazy_threshold_bytes or self._DEFAULT_LAZY_THRESHOLD_BYTES
        )
        self._timeline = Timeline()
        self._proxy_scene = ProxyScene(self._timeline, self._lazy_threshold_bytes)
        self._playback_thread: threading.Thread | None = None
        self._playback_stop = threading.Event()
        self._current_time = 0
        self._fps = self._DEFAULT_FPS
        super().__init__(
            host=host,
            port=port,
            label=label,
            verbose=verbose,
            enable_webxr=enable_webxr,
            ssl_context=ssl_context,
            **kwargs,
        )
        self._renderer = SceneRenderer(self._timeline, self._live_scene)
        self._playback_controls = PlaybackControls(self)

    @property
    def scene(self) -> ProxyScene | SceneApi:
        return self._proxy_scene if self._recording else self._live_scene

    @scene.setter
    def scene(self, value: SceneApi) -> None:
        self._live_scene = value
        self._renderer = SceneRenderer(self._timeline, value)

    @contextmanager
    def at(self, t: int) -> Iterator[None]:
        assert 0 <= t < self.num_steps
        self._recording = True
        self._proxy_scene.set_time(t)
        try:
            yield
        finally:
            self._recording = False
            self._proxy_scene.set_time(None)

    def play(self, fps: float, loop: bool = False) -> None:
        self._set_fps(fps)
        if self._playback_thread is not None and self._playback_thread.is_alive():
            return

        self._playback_controls.set_playing(True)
        self._playback_stop = threading.Event()
        self._playback_thread = threading.Thread(
            target=self._playback_loop, args=(loop,), daemon=True
        )
        self._playback_thread.start()

    def pause(self) -> None:
        if self._playback_thread is not None and self._playback_thread.is_alive():
            self._playback_stop.set()
            self._playback_thread.join()
        self._playback_controls.set_playing(False)

    def seek(self, t: int) -> None:
        assert 0 <= t < self.num_steps
        self.pause()
        self._current_time = t
        self._renderer.apply(t)
        self._playback_controls.set_time(t)

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
        self._playback_controls.set_time(t)
        return True

    def _set_fps(self, fps: float) -> None:
        self._fps = fps
        self._playback_controls.set_fps(fps)


# =============================================================================
# Recording proxies
# =============================================================================


class ProxyScene:
    """Scene proxy that records operations to a timeline."""

    def __init__(self, timeline: Timeline, lazy_threshold_bytes: int) -> None:
        self._timeline = timeline
        self._lazy_threshold_bytes = lazy_threshold_bytes
        self._recording_time: int | None = None

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
    """Handle proxy that records attribute assignments."""

    __slots__ = ("_parent_scene", "_name")

    def __init__(self, parent_scene: ProxyScene, name: str) -> None:
        self._parent_scene = parent_scene
        self._name = name

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        op = Op.create(
            kind=OpKind.SET,
            target=self._name,
            member=name,
            args=(value,),
            threshold_bytes=self._parent_scene._lazy_threshold_bytes,
        )
        self._parent_scene.record(op)

    def remove(self) -> None:
        op = Op.create(
            kind=OpKind.REMOVE,
            target=self._name,
            member="remove",
            threshold_bytes=self._parent_scene._lazy_threshold_bytes,
        )
        self._parent_scene.record(op)


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
