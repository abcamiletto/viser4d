from typing import Any

from viser4d._state import (
    SceneEntryRecord,
    SceneState,
    StepDelta,
    StoredMessage,
    materialize,
    scene_puts_deletes,
)


def _stored(**payload: Any) -> StoredMessage:
    return StoredMessage(dict(payload))


def test_key_derivation() -> None:
    create, _ = scene_puts_deletes(_stored(type="FrameMessage", name="/a", props={}))
    assert create[0][0] == "create:/a"

    update, _ = scene_puts_deletes(
        _stored(type="SceneNodeUpdateMessage", name="/a", updates={"x": 1, "y": 2})
    )
    assert [k for k, _n, _m in update] == ["update:/a:x", "update:/a:y"]

    other, _ = scene_puts_deletes(
        _stored(type="SetPositionMessage", name="/a", position=[0, 0, 0])
    )
    assert other[0][0] == "SetPositionMessage:/a"

    boned, _ = scene_puts_deletes(
        _stored(type="SetBoneMessage", name="/a", bone_index=3)
    )
    assert boned[0][0] == "SetBoneMessage:/a:3"

    glob, _ = scene_puts_deletes(_stored(type="SetBackgroundImageMessage"))
    assert glob[0][0] == "SetBackgroundImageMessage"
    assert glob[0][1] is None

    _puts, deletes = scene_puts_deletes(
        _stored(type="RemoveSceneNodeMessage", name="/a")
    )
    assert deletes == ["/a"]


def test_step_delta_recreate_drops_own_props_but_keeps_descendants() -> None:
    delta = StepDelta()
    delta.fold_put(
        SceneEntryRecord(
            "create:/a", 1, "/a", _stored(type="FrameMessage", name="/a", props={})
        )
    )
    delta.fold_put(
        SceneEntryRecord(
            "SetPositionMessage:/a",
            2,
            "/a",
            _stored(type="SetPositionMessage", name="/a"),
        )
    )
    delta.fold_put(
        SceneEntryRecord(
            "create:/a/child",
            3,
            "/a/child",
            _stored(type="FrameMessage", name="/a/child", props={}),
        )
    )
    delta.fold_put(
        SceneEntryRecord(
            "create:/a", 4, "/a", _stored(type="FrameMessage", name="/a", props={})
        )
    )
    assert "SetPositionMessage:/a" not in delta.puts
    assert "create:/a/child" in delta.puts
    assert delta.puts["create:/a"].rev == 4


def test_scene_state_delete_drops_descendants() -> None:
    state = SceneState()
    state.put(
        SceneEntryRecord(
            "create:/a", 1, "/a", _stored(type="FrameMessage", name="/a", props={})
        )
    )
    state.put(
        SceneEntryRecord(
            "create:/a/b",
            2,
            "/a/b",
            _stored(type="FrameMessage", name="/a/b", props={}),
        )
    )
    state.put(
        SceneEntryRecord(
            "create:/c", 3, "/c", _stored(type="FrameMessage", name="/c", props={})
        )
    )
    state.delete_node("/a")
    assert state.node_names() == {"/c"}


def test_materialize_orders_parents_before_children() -> None:
    entries = [
        SceneEntryRecord(
            "SetPositionMessage:/root/child",
            1,
            "/root/child",
            _stored(type="SetPositionMessage", name="/root/child"),
        ),
        SceneEntryRecord(
            "create:/root/child",
            2,
            "/root/child",
            _stored(type="FrameMessage", name="/root/child", props={}),
        ),
        SceneEntryRecord(
            "create:/root",
            3,
            "/root",
            _stored(type="FrameMessage", name="/root", props={}),
        ),
    ]
    result = materialize(entries, [], [])
    assert [(m.payload["type"], m.payload.get("name")) for m in result] == [
        ("FrameMessage", "/root"),
        ("FrameMessage", "/root/child"),
        ("SetPositionMessage", "/root/child"),
    ]
