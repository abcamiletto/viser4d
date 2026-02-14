from __future__ import annotations

from viser4d.op import Op, OpKind
from viser4d.timeline import Timeline


def _make_add_op(target: str) -> Op:
    return Op.create(
        kind=OpKind.ADD,
        target=target,
        member="add_frame",
        args=(target,),
        kwargs={"axes_length": 0.1},
    )


def test_diff_between_none_and_first_state_creates_node() -> None:
    timeline = Timeline()
    timeline.record_add(0, "/a", _make_add_op("/a"))
    timeline.record_set(0, "/a", "position", (0.0, 0.0, 0.0))

    diff = timeline.diff_between(None, 0)

    assert diff.nodes_to_remove == set()
    assert set(diff.nodes_to_create_or_replace) == {"/a"}
    assert diff.member_updates == {}
    assert diff.nodes_to_create_or_replace["/a"].set_values["position"].value == (
        0.0,
        0.0,
        0.0,
    )


def test_diff_between_adjacent_steps_returns_only_member_updates() -> None:
    timeline = Timeline()
    timeline.record_add(0, "/a", _make_add_op("/a"))
    timeline.record_set(0, "/a", "position", (0.0, 0.0, 0.0))
    timeline.record_set(1, "/a", "position", (1.0, 0.0, 0.0))

    diff = timeline.diff_between(0, 1)

    assert diff.nodes_to_remove == set()
    assert diff.nodes_to_create_or_replace == {}
    assert set(diff.member_updates) == {"/a"}
    assert diff.member_updates["/a"]["position"].value == (1.0, 0.0, 0.0)


def test_diff_between_handles_remove_and_readd() -> None:
    timeline = Timeline()
    timeline.record_add(0, "/a", _make_add_op("/a"))
    timeline.record_remove(1, "/a")
    timeline.record_add(2, "/a", _make_add_op("/a"))

    removed = timeline.diff_between(0, 1)
    recreated = timeline.diff_between(1, 2)

    assert removed.nodes_to_remove == {"/a"}
    assert removed.nodes_to_create_or_replace == {}
    assert removed.member_updates == {}

    assert recreated.nodes_to_remove == set()
    assert set(recreated.nodes_to_create_or_replace) == {"/a"}
    assert recreated.nodes_to_create_or_replace["/a"].add_index == 1
    assert recreated.member_updates == {}


def test_diff_between_backward_recreates_when_member_would_be_unset() -> None:
    timeline = Timeline()
    timeline.record_add(0, "/a", _make_add_op("/a"))
    timeline.record_set(1, "/a", "position", (1.0, 0.0, 0.0))

    diff = timeline.diff_between(1, 0)

    assert diff.nodes_to_remove == set()
    assert set(diff.nodes_to_create_or_replace) == {"/a"}
    assert diff.member_updates == {}
