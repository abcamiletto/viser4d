from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .op import Op

if TYPE_CHECKING:
    from viser import SceneApi


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
        return None if index == 0 else index - 1


@dataclass
class _NodeTrack:
    adds: _TimeSeries = field(default_factory=_TimeSeries)
    removes: _TimeSeries = field(default_factory=_TimeSeries)
    sets: dict[str, _TimeSeries] = field(default_factory=dict)


@dataclass
class _RenderedNode:
    handle: Any
    add_index: int
    set_indices: dict[str, int] = field(default_factory=dict)


class Timeline:
    """Stores temporal operation data."""

    def __init__(self) -> None:
        self._tracks: dict[str, _NodeTrack] = {}

    def _track(self, target: str) -> _NodeTrack:
        return self._tracks.setdefault(target, _NodeTrack())

    def record_add(self, t: int, target: str, op: Op) -> None:
        self._track(target).adds.add(t, op)

    def record_remove(self, t: int, target: str) -> None:
        self._track(target).removes.add(t, None)

    def record_set(self, t: int, target: str, member: str, value: Any) -> None:
        self._track(target).sets.setdefault(member, _TimeSeries()).add(t, value)

    @property
    def targets(self) -> set[str]:
        return set(self._tracks)

    def get_add_at(self, target: str, t: int) -> tuple[int, Op, int] | None:
        """Return (add_index, op, add_time) if target should exist at time t."""
        track = self._tracks.get(target)
        if track is None:
            return None

        add_index = track.adds.latest_index(t)
        if add_index is None:
            return None

        add_time = track.adds.times[add_index]
        remove_index = track.removes.latest_index(t)
        if remove_index is not None and track.removes.times[remove_index] >= add_time:
            return None

        return (add_index, track.adds.values[add_index], add_time)


class SceneRenderer:
    """Applies timeline state to a live scene."""

    def __init__(self, timeline: Timeline, scene: SceneApi) -> None:
        self._timeline = timeline
        self._scene = scene
        self._nodes: dict[str, _RenderedNode] = {}
        self._rendered_time: int = -1

    def apply(self, t: int) -> None:
        if t < self._rendered_time:
            self.reset()
        self._apply_state(t)
        self._rendered_time = t

    def reset(self) -> None:
        """Clear all rendered state."""
        for target in list(self._nodes):
            self._scene.remove_by_name(target)
        self._nodes.clear()
        self._rendered_time = -1

    def _apply_state(self, t: int) -> None:
        for target in self._timeline.targets | set(self._nodes):
            add_info = self._timeline.get_add_at(target, t)
            if add_info is None:
                if target in self._nodes:
                    self._scene.remove_by_name(target)
                    self._nodes.pop(target, None)
                continue

            add_index, op, add_time = add_info
            node = self._nodes.get(target)
            if node is None or node.add_index != add_index:
                if node is not None:
                    self._scene.remove_by_name(target)
                node = _RenderedNode(
                    handle=getattr(self._scene, op.member)(*op.args, **op.kwargs),
                    add_index=add_index,
                )
                self._nodes[target] = node

            for member, series in self._timeline._tracks[target].sets.items():
                set_index = series.latest_index(t)
                if set_index is None or series.times[set_index] < add_time:
                    continue
                if node.set_indices.get(member) != set_index:
                    setattr(node.handle, member, series.values[set_index])
                    node.set_indices[member] = set_index
