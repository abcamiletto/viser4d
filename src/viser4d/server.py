import dataclasses
import threading
import time
from bisect import bisect_right
from contextlib import contextmanager
from enum import Enum
from typing import Any, Iterator

import viser as _viser
from viser import SceneApi

from .gui import PlaybackControls

class OpKind(Enum):
    ADD = "add"
    REMOVE = "remove"
    SET = "set"


@dataclasses.dataclass(frozen=True)
class Op:
    """Recorded scene operation for later playback."""

    kind: OpKind
    target: str
    member: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = dataclasses.field(default_factory=dict)


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


class ProxyHandle:
    """Handle proxy that records attribute assignments."""

    __slots__ = ("_parent_scene", "_name")

    def __init__(self, parent_scene: "ProxyScene", name: str) -> None:
        self._parent_scene = parent_scene
        self._name = name

    def __setattr__(self, name: str, value: Any) -> None:
        # Avoid errors when setting private attributes in __init__
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return

        # Record the attribute assignment
        op = Op(kind=OpKind.SET, target=self._name, member=name, args=(value,))
        self._parent_scene.record(op)

    def remove(self) -> None:
        op = Op(kind=OpKind.REMOVE, target=self._name, member="remove")
        self._parent_scene.record(op)


class ProxyScene:
    """Scene proxy that records add/attribute operations across time."""

    def __init__(self, scene: SceneApi | None, num_steps: int) -> None:
        self._recording_time: int | None = None
        self._adds: dict[str, _TimeSeries] = {}
        self._removes: dict[str, _TimeSeries] = {}
        self._sets: dict[str, dict[str, _TimeSeries]] = {}
        self._live_scene = scene
        self._handles: dict[str, Any] = {}
        self._applied_up_to: int = -1
        self._applied_add_index: dict[str, int] = {}
        self._applied_set_index: dict[str, dict[str, int]] = {}

    def __getattr__(self, name: str) -> Any:
        # If it is an add_ method, record it and cache it
        if name.startswith("add_"):

            def _add(*args: Any, **kwargs: Any) -> ProxyHandle:
                target = self._target_from_add(args, kwargs)
                op = Op(
                    kind=OpKind.ADD,
                    target=target,
                    member=name,
                    args=args,
                    kwargs=kwargs,
                )
                self.record(op)
                return ProxyHandle(self, target)

            return _add

        if name == "remove_by_name":

            def _remove(target: str) -> None:
                self.record(Op(kind=OpKind.REMOVE, target=target, member=name))

            return _remove

        # Otherwise, forward the call to the real scene
        def _call(*args: Any, **kwargs: Any) -> None:
            return getattr(self._live_scene, name)(*args, **kwargs)

        return _call

    def set_time(self, time_step: int | None) -> None:
        self._recording_time = time_step

    def record(self, op: Op) -> None:
        if self._recording_time is None:
            raise RuntimeError("Cannot record operation when time is not set.")
        if op.kind is OpKind.ADD:
            series = self._adds.setdefault(op.target, _TimeSeries())
            series.add(self._recording_time, op)
        elif op.kind is OpKind.REMOVE:
            series = self._removes.setdefault(op.target, _TimeSeries())
            series.add(self._recording_time, None)
        else:
            target_sets = self._sets.setdefault(op.target, {})
            series = target_sets.setdefault(op.member, _TimeSeries())
            series.add(self._recording_time, op.args[0])

    @staticmethod
    def _target_from_add(args: tuple[Any, ...], kwargs: dict[str, Any]) -> str:
        return kwargs.get("name", args[0])

    def apply(self, scene: SceneApi, t: int) -> None:
        if t <= self._applied_up_to:
            self._reset(scene)
        self._apply_state(scene, t)
        self._applied_up_to = t

    def _reset(self, scene: SceneApi) -> None:
        for target in self._handles:
            scene.remove_by_name(target)
        self._handles.clear()
        self._applied_add_index.clear()
        self._applied_set_index.clear()
        self._applied_up_to = -1

    def _apply_state(self, scene: SceneApi, t: int) -> None:
        targets = set(self._adds) | set(self._handles)
        for target in targets:
            series = self._adds.get(target)
            if series is None:
                if target in self._handles:
                    scene.remove_by_name(target)
                    self._handles.pop(target, None)
                    self._applied_add_index.pop(target, None)
                    self._applied_set_index.pop(target, None)
                continue

            add_index = series.latest_index(t)
            if add_index is None:
                if target in self._handles:
                    scene.remove_by_name(target)
                    self._handles.pop(target, None)
                    self._applied_add_index.pop(target, None)
                    self._applied_set_index.pop(target, None)
                continue

            remove_series = self._removes.get(target)
            if remove_series is not None:
                remove_index = remove_series.latest_index(t)
                if remove_index is not None:
                    remove_time = remove_series.times[remove_index]
                    if remove_time >= series.times[add_index]:
                        if target in self._handles:
                            scene.remove_by_name(target)
                            self._handles.pop(target, None)
                            self._applied_add_index.pop(target, None)
                            self._applied_set_index.pop(target, None)
                        continue

            if self._applied_add_index.get(target) != add_index:
                if target in self._handles:
                    scene.remove_by_name(target)
                op = series.values[add_index]
                self._handles[target] = getattr(self._live_scene, op.member)(
                    *op.args, **op.kwargs
                )
                self._applied_add_index[target] = add_index
                self._applied_set_index[target] = {}

            add_time = series.times[add_index]
            handle = self._handles[target]
            for member, member_series in self._sets.get(target, {}).items():
                member_index = member_series.latest_index(t)
                if member_index is None:
                    continue
                if member_series.times[member_index] < add_time:
                    continue
                applied = self._applied_set_index[target].get(member)
                if applied != member_index:
                    setattr(handle, member, member_series.values[member_index])
                    self._applied_set_index[target][member] = member_index


class PlaybackController:
    """Controller for playing back recorded timelines."""

    def __init__(self, server: "ViserServer", fps: float, enable_gui: bool) -> None:
        self._server = server
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._current = 0
        self._fps = fps
        self._controls = (
            PlaybackControls(
                server.gui,
                server.num_steps,
                fps,
                on_seek=self.seek,
                on_play=self.play,
                on_pause=self.pause,
                on_fps=self._set_fps,
            )
            if enable_gui
            else None
        )

    @property
    def current_time(self) -> int:
        return self._current

    def play(self, fps: float | None = None, loop: bool = False) -> None:
        if fps is not None:
            self._set_fps(fps)
        if self._thread is not None and self._thread.is_alive():
            return

        def _run() -> None:
            index = self._current
            last_fps = self._fps
            frame_duration = 1.0 / last_fps
            next_time = time.monotonic()
            while True:
                while index < self._server.num_steps:
                    if self._stop_event.is_set():
                        return
                    if self._fps != last_fps:
                        last_fps = self._fps
                        frame_duration = 1.0 / last_fps
                        next_time = time.monotonic()
                    now = time.monotonic()
                    if now > next_time + frame_duration:
                        # Skip ahead to avoid stacking frame updates when lagging.
                        skip = int((now - next_time) // frame_duration)
                        index = min(index + skip, self._server.num_steps - 1)
                        next_time += skip * frame_duration
                    if not self._step(index):
                        return
                    index += 1
                    next_time += frame_duration
                    delay = next_time - time.monotonic()
                    if delay > 0 and self._stop_event.wait(timeout=delay):
                        return
                if not loop:
                    break
                index = 0
                next_time = time.monotonic()
            if self._controls is not None:
                self._controls.set_playing(False)

        if self._controls is not None:
            self._controls.set_playing(True)
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()

    def pause(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            self._stop_event.set()
            self._thread.join()
        if self._controls is not None:
            self._controls.set_playing(False)

    def seek(self, t: int) -> None:
        self.pause()
        self._current = t
        self._apply(t)
        if self._controls is not None:
            self._controls.set_time(t)

    def _step(self, t: int) -> bool:
        """Apply frame and return False if stopped."""
        if self._stop_event.is_set():
            return False
        self._apply(t)
        self._current = t
        if self._controls is not None:
            self._controls.set_time(t)
        return True

    def _apply(self, t: int) -> None:
        self._server._scene_recording.apply(self._server._live_scene, t)

    def _set_fps(self, fps: float) -> None:
        self._fps = fps
        if self._controls is not None:
            self._controls.set_fps(fps)


class ViserServer(_viser.ViserServer):
    """Viser server with timeline recording and playback controls."""

    def __init__(
        self,
        num_steps: int,
        host: str = "0.0.0.0",
        port: int = 8080,
        label: str | None = None,
        verbose: bool = True,
        enable_webxr: bool = False,
        ssl_context=None,
        *,
        fps: float = 30.0,
        enable_playback_gui: bool = True,
        **kwargs,
    ) -> None:
        self._recording = False
        self.num_steps = num_steps
        self.fps = fps
        self._scene_recording = ProxyScene(None, num_steps)
        super().__init__(
            host=host,
            port=port,
            label=label,
            verbose=verbose,
            enable_webxr=enable_webxr,
            ssl_context=ssl_context,
            **kwargs,
        )
        self.playback = PlaybackController(self, fps, enable_playback_gui)

    @property
    def scene(self) -> ProxyScene | SceneApi:
        return self._scene_recording if self._recording else self._live_scene

    @scene.setter
    def scene(self, value: SceneApi) -> None:
        self._live_scene = value
        self._scene_recording._live_scene = value
        self._scene_recording._handles.clear()
        self._scene_recording._applied_up_to = -1

    @contextmanager
    def at(self, t: int) -> Iterator[None]:
        assert 0 <= t < self.num_steps
        self._recording = True
        self._scene_recording.set_time(t)
        try:
            yield
        finally:
            self._recording = False
            self._scene_recording.set_time(None)

    def play(self, fps: float | None = None, loop: bool = False) -> None:
        self.playback.play(fps=fps, loop=loop)

    def pause(self) -> None:
        self.playback.pause()

    def seek(self, t: int) -> None:
        assert 0 <= t < self.num_steps
        self.playback.seek(t)
