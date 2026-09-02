import base64
import re
from typing import cast

import numpy as np
import pytest
from helpers import deserialize_recording

import viser4d
from viser4d import _build as build_module
from viser4d import _export, _viser


def test_timeline_operations_serialize_and_playback_commands() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            frame = tl.scene.add_frame("/frame")
            audio = tl.audio.add_track(
                "/audio", data=np.array([0, 1, 2], dtype=np.int16), sample_rate=16_000
            )
        with server.at(1):
            frame.position = (1.0, 2.0, 3.0)
            audio.volume = 0.25
        server.set_playback_speed(0.8)
        server.play()
        server.pause()
        assert server.serialize()
        assert server.serialize(start_timestep=1, end_timestep=1)
    finally:
        server.stop()


def test_serialize_rejects_invalid_range() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with pytest.raises(
            ValueError, match="start_timestep must be less than or equal"
        ):
            server.serialize(start_timestep=2, end_timestep=1)
    finally:
        server.stop()


def test_removed_static_nodes_serialize_as_removals() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        joint = server.scene.add_frame("/joint")
        joint.position = (1.0, 2.0, 3.0)
        joint.remove()
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        joint_messages = [m for _, m in messages if m.get("name") == "/joint"]
        assert joint_messages[-1]["type"] == "RemoveSceneNodeMessage"
    finally:
        server.stop()


def test_as_html_embeds_native_recording() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            tl.scene.add_frame("/joint")
        html = server.as_html()
        match = re.search(r'window\.__VISER_EMBED_DATA__="([^"]+)"', html)
        assert match is not None
        recording = deserialize_recording(base64.b64decode(match.group(1)))
        assert isinstance(recording["messages"], list)
    finally:
        server.stop()


def test_runtime_bootstrap_is_serialized_with_export() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        runtime = [m for _, m in messages if m.get("type") == "RunJavascriptMessage"]
        assert len(runtime) == 1
        assert str(runtime[0]["source"]).startswith(build_module.RUNTIME_MARKER)
    finally:
        server.stop()


def test_timeline_handle_updates_become_overrides() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            joint = tl.scene.add_frame("/joint")
        joint.position = (2.0, 0.0, 0.0)
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        positions = [
            (t, tuple(cast(list[float], m["position"])))
            for t, m in messages
            if m.get("type") == "SetPositionMessage" and m.get("name") == "/joint"
        ]
        assert positions == [
            (0.0, (2.0, 0.0, 0.0)),
            (1.0 / server.fps, (2.0, 0.0, 0.0)),
        ]
    finally:
        server.stop()


def test_overrides_reapply_after_recorded_updates() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            joint = tl.scene.add_frame("/joint")
        with server.at(1):
            joint.position = (1.0, 0.0, 0.0)
        joint.position = (2.0, 0.0, 0.0)
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        positions = [
            (t, tuple(cast(list[float], m["position"])))
            for t, m in messages
            if m.get("type") == "SetPositionMessage" and m.get("name") == "/joint"
        ]
        assert positions == [
            (0.0, (2.0, 0.0, 0.0)),
            (1.0 / server.fps, (1.0, 0.0, 0.0)),
            (1.0 / server.fps, (2.0, 0.0, 0.0)),
        ]
    finally:
        server.stop()


def test_overrides_wait_for_late_created_nodes_in_export() -> None:
    server = viser4d.Viser4dServer(num_steps=3, fps=2.0, port=0, verbose=False)
    try:
        with server.at(1) as tl:
            joint = tl.scene.add_frame("/joint")
        joint.position = (2.0, 0.0, 0.0)
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        positions = [
            (t, tuple(cast(list[float], m["position"])))
            for t, m in messages
            if m.get("type") == "SetPositionMessage" and m.get("name") == "/joint"
        ]
        assert positions == [
            (1.0 / server.fps, (2.0, 0.0, 0.0)),
            (2.0 / server.fps, (2.0, 0.0, 0.0)),
        ]
    finally:
        server.stop()


def test_export_remaps_override_buffers_once() -> None:
    server = viser4d.Viser4dServer(num_steps=8, fps=1.0, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            cloud = tl.scene.add_point_cloud(
                "/cloud",
                points=np.zeros((4, 3), dtype=np.float32),
                colors=np.zeros((4, 3), dtype=np.uint8),
            )
        cloud.points = np.ones((4, 3), dtype=np.float32)  # override with an array

        one_step = _export.build(
            server.get_scene_serializer(), server._timeline, server.fps, 0, 0
        )
        all_steps = _export.build(
            server.get_scene_serializer(), server._timeline, server.fps, 0, None
        )
        # The override applies at every step but its buffers land exactly once.
        assert len(_viser.serializer_binary_buffers(all_steps)) == len(
            _viser.serializer_binary_buffers(one_step)
        )
    finally:
        server.stop()
