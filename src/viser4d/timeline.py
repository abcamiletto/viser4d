from __future__ import annotations

from bisect import bisect_right
from typing import TYPE_CHECKING, Any

from .op import Op

if TYPE_CHECKING:
    from viser import SceneApi


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
            if remove_index is not None and remove_series.times[remove_index] >= add_time:
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
