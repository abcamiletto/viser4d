from typing import Any, cast

import pytest
from helpers import deserialize_recording

import viser4d
from viser4d._recorder import Recorder
from viser4d._state import SceneEntryRecord


def test_at_keeps_server_scene_live() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            joint = tl.scene.add_frame("/joint")
            server.scene.add_frame("/static")
            joint.position = (2.0, 0.0, 0.0)
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        creation = [
            t
            for t, m in messages
            if m.get("type") == "FrameMessage" and m.get("name") == "/joint"
        ]
        positions = [
            tuple(cast(list[float], m["position"]))
            for _, m in messages
            if m.get("type") == "SetPositionMessage" and m.get("name") == "/joint"
        ]
        static = [
            t
            for t, m in messages
            if m.get("type") == "FrameMessage" and m.get("name") == "/static"
        ]
        assert creation == [0.0]
        assert positions == [(2.0, 0.0, 0.0)]
        assert static == [0.0]
    finally:
        server.stop()


def test_same_step_scene_updates_serialize_latest_value_once() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            joint = tl.scene.add_frame("/joint")
        with server.at(0):
            joint.position = (1.0, 0.0, 0.0)
        with server.at(0):
            joint.position = (2.0, 0.0, 0.0)
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        positions = [
            tuple(cast(list[float], m["position"]))
            for _, m in messages
            if m.get("type") == "SetPositionMessage" and m.get("name") == "/joint"
        ]
        assert positions == [(2.0, 0.0, 0.0)]
    finally:
        server.stop()


def test_at_rejects_nested_sessions() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with (
            pytest.raises(RuntimeError, match="cannot be nested"),
            server.at(0),
            server.at(1),
        ):
            pass
    finally:
        server.stop()


def test_at_rejects_static_name_collisions() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        server.scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))
        with pytest.raises(RuntimeError, match="static scene node"), server.at(0) as tl:
            tl.scene.add_icosphere("/joint", position=(1.0, 0.0, 0.0))
    finally:
        server.stop()


def test_at_allows_recreating_timeline_nodes() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            tl.scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))
        with server.at(1) as tl:
            joint = tl.scene.add_icosphere("/joint", position=(1.0, 0.0, 0.0))
            joint.position = (2.0, 0.0, 0.0)
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        creation = [
            t
            for t, m in messages
            if m.get("type") == "IcosphereMessage" and m.get("name") == "/joint"
        ]
        positions = [
            (t, tuple(cast(list[float], m["position"])))
            for t, m in messages
            if m.get("type") == "SetPositionMessage" and m.get("name") == "/joint"
        ]
        assert creation == [0.0, 1.0 / server.fps]
        assert positions == [(1.0 / server.fps, (2.0, 0.0, 0.0))]
    finally:
        server.stop()


def test_timeline_scene_creation_requires_timestep_context() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            scene = tl.scene
        with pytest.raises(
            RuntimeError, match="creation is only valid inside server.at\\(t\\)"
        ):
            scene.add_frame("/joint")
    finally:
        server.stop()


def test_late_created_nodes_keep_creation_step() -> None:
    server = viser4d.Viser4dServer(num_steps=6, fps=1.0, port=0, verbose=False)
    try:
        with server.at(5) as tl:
            joint = tl.scene.add_frame("/joint")
            joint.position = (2.0, 0.0, 0.0)
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        creation = [
            t
            for t, m in messages
            if m.get("type") == "FrameMessage" and m.get("name") == "/joint"
        ]
        position = [
            t
            for t, m in messages
            if m.get("type") == "SetPositionMessage" and m.get("name") == "/joint"
        ]
        assert creation == [5.0]
        assert position == [5.0]
    finally:
        server.stop()


def test_live_scene_removals_forwarded_without_block_refresh() -> None:
    server = viser4d.Viser4dServer(num_steps=4, port=0, verbose=False)
    refreshed_blocks: list[int] = []
    overrides: list[SceneEntryRecord] = []
    recorder = Recorder(
        server,
        server._timeline,
        on_override=overrides.extend,
        on_block_change=refreshed_blocks.append,
    )
    try:
        with recorder.at(0) as tl:
            frame = tl.scene.add_frame("/frame")
        assert refreshed_blocks == [0]

        refreshed_blocks.clear()
        frame.remove()

        assert refreshed_blocks == []
        assert len(overrides) == 1
        assert overrides[0].key == "RemoveSceneNodeMessage:/frame"
        assert overrides[0].message.payload["type"] == "RemoveSceneNodeMessage"
    finally:
        server.stop()


def test_set_steps_can_grow_timeline() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(1) as tl:
            joint = tl.scene.add_frame("/joint")
        server.set_steps(4)
        with server.at(3):
            joint.position = (3.0, 0.0, 0.0)
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        positions = [
            tuple(cast(list[float], m["position"]))
            for _, m in messages
            if m.get("type") == "SetPositionMessage" and m.get("name") == "/joint"
        ]
        assert positions == [(3.0, 0.0, 0.0)]
    finally:
        server.stop()


def test_set_steps_can_shrink_timeline() -> None:
    server = viser4d.Viser4dServer(num_steps=4, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            joint = tl.scene.add_frame("/joint")
        with server.at(3):
            joint.position = (3.0, 0.0, 0.0)
        server.set_steps(2)
        with pytest.raises(IndexError, match="out of range"), server.at(3):
            pass
        with pytest.raises(ValueError, match="start_timestep must be in \\[0, 1\\]"):
            server.serialize(start_timestep=2, end_timestep=2)
    finally:
        server.stop()


def test_set_steps_rejects_active_recording() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with (
            pytest.raises(
                RuntimeError, match="cannot run while inside server.at\\(t\\)"
            ),
            server.at(0),
        ):
            server.set_steps(3)
    finally:
        server.stop()


def test_clear_resets_timeline_and_shared_scene() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        server.scene.add_frame("/static")
        with server.at(0) as tl:
            tl.scene.add_frame("/joint")
        server.clear()
        assert server.scene.get_handle_by_name("/static") is None
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        assert all(m.get("name") != "/joint" for _, m in messages)

        with server.at(1) as tl:
            tl.scene.add_frame("/joint")
        recording = deserialize_recording(
            server.serialize(start_timestep=1, end_timestep=1)
        )
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        creations = [
            m
            for _, m in messages
            if m.get("type") == "FrameMessage" and m.get("name") == "/joint"
        ]
        assert len(creations) == 1
    finally:
        server.stop()


def test_clear_rejects_active_recording() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with (
            pytest.raises(
                RuntimeError, match="cannot run while inside server.at\\(t\\)"
            ),
            server.at(0),
        ):
            server.clear()
    finally:
        server.stop()


def test_serialization_survives_block_eviction_to_disk() -> None:
    server = viser4d.Viser4dServer(num_steps=300, fps=1.0, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            joint = tl.scene.add_frame("/joint")
        for step in (1, 64, 128, 192, 256):
            with server.at(step):
                joint.position = (float(step), 0.0, 0.0)
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        position_times = [
            t for t, m in messages if m.get("type") == "SetPositionMessage"
        ]
        positions = [
            tuple(cast(list[float], m["position"]))
            for _, m in messages
            if m.get("type") == "SetPositionMessage"
        ]
        assert position_times == [1.0, 64.0, 128.0, 192.0, 256.0]
        assert positions == [(float(s), 0.0, 0.0) for s in (1, 64, 128, 192, 256)]
    finally:
        server.stop()


def _checkpoint_position(message: Any, name: str) -> tuple[float, ...]:
    for entry in message.checkpointScene:
        payload = entry["message"]
        if payload.get("type") == "SetPositionMessage" and payload.get("name") == name:
            return tuple(float(v) for v in payload["position"])
    raise AssertionError(f"Missing checkpoint position for {name!r}.")


def test_delete_only_edit_invalidates_later_checkpoints() -> None:
    server = viser4d.Viser4dServer(
        num_steps=4,
        streaming=viser4d.StreamingConfig(block_size=2),
        port=0,
        verbose=False,
    )
    try:
        with server.at(0) as tl:
            frame = tl.scene.add_frame("/joint")
        with server.at(2) as tl:
            tl.scene.add_frame("/other")

        # Cache block 1's checkpoint; /joint exists at its boundary.
        names = [e["name"] for e in server._timeline.block_message(1).checkpointScene]
        assert "/joint" in names

        # A delete-only edit in block 0 must invalidate that cached checkpoint.
        with server.at(0):
            frame.remove()

        names = [e["name"] for e in server._timeline.block_message(1).checkpointScene]
        assert "/joint" not in names
    finally:
        server.stop()


def test_block_checkpoint_rebuilds_after_earlier_edit() -> None:
    server = viser4d.Viser4dServer(
        num_steps=3,
        fps=1.0,
        streaming=viser4d.StreamingConfig(block_size=1),
        port=0,
        verbose=False,
    )
    try:
        with server.at(0) as tl:
            joint = tl.scene.add_frame("/joint")
        with server.at(1):
            joint.position = (1.0, 0.0, 0.0)
        with server.at(2):
            joint.position = (2.0, 0.0, 0.0)

        before = server._timeline.block_message(2)
        assert _checkpoint_position(before, "/joint") == (1.0, 0.0, 0.0)

        with server.at(1):
            joint.position = (5.0, 0.0, 0.0)

        after = server._timeline.block_message(2)
        assert _checkpoint_position(after, "/joint") == (5.0, 0.0, 0.0)
    finally:
        server.stop()
