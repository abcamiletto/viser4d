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


@dataclass(frozen=True)
class _TimelineValueState:
    version: int
    value: Any


@dataclass(frozen=True)
class _TimelineNodeState:
    add_index: int
    add_op: Op
    set_values: dict[str, _TimelineValueState]


@dataclass(frozen=True)
class _TimelineDiff:
    nodes_to_remove: set[str]
    nodes_to_create_or_replace: dict[str, _TimelineNodeState]
    member_updates: dict[str, dict[str, _TimelineValueState]]


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

    def state_at(self, t: int) -> dict[str, _TimelineNodeState]:
        """Return the full renderable state for timestep ``t``."""
        state_by_target: dict[str, _TimelineNodeState] = {}
        for target, track in self._tracks.items():
            add_info = self._resolve_add(track, t)
            if add_info is None:
                continue

            add_index, add_op, add_time = add_info
            set_values: dict[str, _TimelineValueState] = {}
            for member, series in track.sets.items():
                set_index = series.latest_index(t)
                if set_index is None or series.times[set_index] < add_time:
                    continue
                set_values[member] = _TimelineValueState(
                    version=set_index,
                    value=series.values[set_index],
                )

            state_by_target[target] = _TimelineNodeState(
                add_index=add_index,
                add_op=add_op,
                set_values=set_values,
            )
        return state_by_target

    def diff_between(self, t_from: int | None, t_to: int) -> _TimelineDiff:
        """Return render changes needed to move from ``t_from`` to ``t_to``."""
        from_state = {} if t_from is None else self.state_at(t_from)
        to_state = self.state_at(t_to)
        return self._diff_states(from_state, to_state)

    @staticmethod
    def _resolve_add(track: _NodeTrack, t: int) -> tuple[int, Op, int] | None:
        add_index = track.adds.latest_index(t)
        if add_index is None:
            return None

        add_time = track.adds.times[add_index]
        remove_index = track.removes.latest_index(t)
        if remove_index is not None and track.removes.times[remove_index] >= add_time:
            return None

        return (add_index, track.adds.values[add_index], add_time)

    @staticmethod
    def _diff_states(
        from_state: dict[str, _TimelineNodeState],
        to_state: dict[str, _TimelineNodeState],
    ) -> _TimelineDiff:
        from_targets = set(from_state)
        to_targets = set(to_state)

        nodes_to_remove = from_targets - to_targets
        nodes_to_create_or_replace: dict[str, _TimelineNodeState] = {}
        member_updates: dict[str, dict[str, _TimelineValueState]] = {}

        for target, to_node in to_state.items():
            from_node = from_state.get(target)
            if from_node is None or from_node.add_index != to_node.add_index:
                nodes_to_create_or_replace[target] = to_node
                continue

            # If any member must be "unset", patching is insufficient: recreate.
            if set(from_node.set_values) - set(to_node.set_values):
                nodes_to_create_or_replace[target] = to_node
                continue

            updates = Timeline._member_updates(from_node, to_node)
            if updates:
                member_updates[target] = updates

        return _TimelineDiff(
            nodes_to_remove=nodes_to_remove,
            nodes_to_create_or_replace=nodes_to_create_or_replace,
            member_updates=member_updates,
        )

    @staticmethod
    def _member_updates(
        from_node: _TimelineNodeState,
        to_node: _TimelineNodeState,
    ) -> dict[str, _TimelineValueState]:
        updates: dict[str, _TimelineValueState] = {}
        for member, to_value in to_node.set_values.items():
            from_value = from_node.set_values.get(member)
            if from_value is None or from_value.version != to_value.version:
                updates[member] = to_value
        return updates


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
        self.apply_diff(
            self._timeline.diff_between(
                None if self._rendered_time < 0 else self._rendered_time,
                t,
            )
        )
        self._rendered_time = t

    def reset(self) -> None:
        """Clear all rendered state."""
        for target in list(self._nodes):
            self._scene.remove_by_name(target)
        self._nodes.clear()
        self._rendered_time = -1

    def apply_diff(self, diff: _TimelineDiff) -> None:
        """Apply a timeline diff to the current rendered scene state."""
        for target in diff.nodes_to_remove:
            self.remove_node(target)
        for target, state in diff.nodes_to_create_or_replace.items():
            self.create_or_replace_node(target, state)
        for target, updates in diff.member_updates.items():
            self.update_node_members(target, updates)

    def remove_node(self, target: str) -> None:
        self._scene.remove_by_name(target)
        self._nodes.pop(target, None)

    def create_or_replace_node(self, target: str, state: _TimelineNodeState) -> None:
        if target in self._nodes:
            self._scene.remove_by_name(target)
        node = _RenderedNode(
            handle=getattr(self._scene, state.add_op.member)(
                *state.add_op.args, **state.add_op.kwargs
            ),
            add_index=state.add_index,
        )
        self._nodes[target] = node
        self._apply_set_values(node, state.set_values)

    def update_node_members(
        self, target: str, updates: dict[str, _TimelineValueState]
    ) -> None:
        node = self._nodes.get(target)
        if node is None:
            raise RuntimeError(
                f"Rendered state missing node {target!r} while applying timeline diff."
            )
        self._apply_set_values(node, updates)

    @staticmethod
    def _apply_set_values(
        node: _RenderedNode, set_values: dict[str, _TimelineValueState]
    ) -> None:
        for member, value_state in set_values.items():
            if node.set_indices.get(member) == value_state.version:
                continue
            setattr(node.handle, member, value_state.value)
            node.set_indices[member] = value_state.version
