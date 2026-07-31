import base64
import re
import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any, cast

import msgspec
import numpy as np
import pytest
import zstandard

import viser4d
from viser4d import _server as server_module
from viser4d import _build as build_module
from viser4d import _viser
from viser4d._playback import ClientSession
from viser4d._protocol import (
    TimelineBlockMessage,
    TimelineManifestsMessage,
    TimelineOverrideMessage,
    TimelinePlayMessage,
    TimelineReadyMessage,
    TimelineSetSpeedMessage,
)
from viser4d._state import (
    SceneEntryRecord,
    SceneState,
    StepDelta,
    StoredMessage,
    materialize,
    scene_puts_deletes,
)


def _deserialize_recording(blob: bytes) -> dict[str, object]:
    inner_size = int.from_bytes(blob[:8], "little")
    inner = zstandard.ZstdDecompressor().decompress(
        blob[8:], max_output_size=inner_size
    )
    assert len(inner) == inner_size
    msgpack_size = int.from_bytes(inner[:8], "little")
    return cast(dict[str, object], msgspec.msgpack.decode(inner[8 : 8 + msgpack_size]))


def _checkpoint_position(message: Any, name: str) -> tuple[float, ...]:
    for entry in message.checkpointScene:
        payload = entry["message"]
        if payload.get("type") == "SetPositionMessage" and payload.get("name") == name:
            return tuple(float(v) for v in payload["position"])
    raise AssertionError(f"Missing checkpoint position for {name!r}.")


# ---------------------------------------------------------------------------
# Configuration and validation
# ---------------------------------------------------------------------------


def test_num_steps_must_be_positive() -> None:
    with pytest.raises(ValueError, match="num_steps must be >= 1"):
        viser4d.Viser4dServer(num_steps=0, port=0, verbose=False)
    server = viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)
    try:
        with pytest.raises(ValueError, match="num_steps must be >= 1"):
            server.set_steps(0)
    finally:
        server.stop()


def test_fps_and_speed_must_be_positive() -> None:
    with pytest.raises(ValueError, match="fps must be a positive finite float"):
        viser4d.Viser4dServer(num_steps=1, fps=0.0, port=0, verbose=False)
    with pytest.raises(
        ValueError, match="playback_speed must be a positive finite float"
    ):
        viser4d.Viser4dServer(num_steps=1, playback_speed=0.0, port=0, verbose=False)
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with pytest.raises(ValueError, match="speed must be a positive finite float"):
            server.set_playback_speed(-1.0)
    finally:
        server.stop()


def test_client_cache_size_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISER4D_CLIENT_CHUNK_CACHE_SIZE", "2MB")
    server = viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)
    try:
        assert server.streaming.client_cache_bytes == 2_000_000
    finally:
        server.stop()


def test_block_size_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISER4D_BLOCK_SIZE", "16")
    server = viser4d.Viser4dServer(num_steps=100, port=0, verbose=False)
    try:
        assert server.block_size == 16
    finally:
        server.stop()


def test_block_size_rejects_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISER4D_BLOCK_SIZE", "invalid")
    with pytest.raises(
        ValueError, match="VISER4D_BLOCK_SIZE must be a positive integer"
    ):
        viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)


def test_client_cache_size_rejects_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISER4D_CLIENT_CHUNK_CACHE_SIZE", "invalid")
    with pytest.raises(
        ValueError,
        match="VISER4D_CLIENT_CHUNK_CACHE_SIZE must be an integer byte count",
    ):
        viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)


def test_streaming_config_is_public_and_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISER4D_BLOCK_SIZE", "16")
    monkeypatch.setenv("VISER4D_CLIENT_CHUNK_CACHE_SIZE", "2MB")
    config = viser4d.StreamingConfig(block_size=8, client_cache_bytes=1234)
    server = viser4d.Viser4dServer(
        num_steps=100, streaming=config, port=0, verbose=False
    )
    try:
        assert server.streaming == config
        assert server.block_size == 8
        assert server.streaming.client_cache_bytes == 1234
    finally:
        server.stop()


def test_server_uses_32_step_blocks_by_default() -> None:
    server = viser4d.Viser4dServer(num_steps=100, port=0, verbose=False)
    try:
        assert server.block_size == 32
    finally:
        server.stop()


def test_play_takes_no_arguments() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    untyped = cast(Any, server)
    try:
        with pytest.raises(TypeError, match="unexpected keyword argument 'loop'"):
            untyped.play(loop=True)
        with pytest.raises(TypeError, match="unexpected keyword argument 'speed'"):
            untyped.play(speed=2.0)
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Server <-> session propagation
# ---------------------------------------------------------------------------


def test_server_playback_config_propagates_to_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = viser4d.Viser4dServer(
        num_steps=2, loop=True, playback_speed=1.5, port=0, verbose=False
    )

    class FakeSession:
        def __init__(self, server: Any, client: Any) -> None:
            self.client_id = client.client_id
            self.loop_on_init = server.loop
            self.speed_on_init = server.playback_speed
            self.config_syncs = 0
            self.play_calls = 0

        def start(self) -> None:
            pass

        def play(self) -> None:
            self.play_calls += 1

        def sync_config(self) -> None:
            self.config_syncs += 1

        def set_speed(self, speed: float) -> None:
            self.speed_on_init = speed

        def handle_event(self, _message: object) -> None:
            pass

    monkeypatch.setattr(server_module, "ClientSession", FakeSession)
    try:
        attach = server._client_connect_cb[-1]
        attach(cast(Any, SimpleNamespace(client_id=123)))
        first = server.get_client_playback(123)
        assert isinstance(first, FakeSession)
        assert first.loop_on_init is True
        assert first.speed_on_init == 1.5

        server.set_loop(False)
        server.set_playback_speed(0.5)
        assert first.config_syncs == 1
        assert first.speed_on_init == 0.5
        assert server.loop is False
        assert server.playback_speed == 0.5

        attach(cast(Any, SimpleNamespace(client_id=456)))
        second = server.get_client_playback(456)
        assert isinstance(second, FakeSession)
        assert second.loop_on_init is False
        assert second.speed_on_init == 0.5

        server.play()
        assert first.play_calls == 1
        assert second.play_calls == 1
    finally:
        server.stop()


def _fake_session_server(**overrides: Any) -> Any:
    base = dict(
        loop=True,
        playback_speed=2.0,
        num_steps=2,
        block_size=32,
        fps=1.0,
        streaming=SimpleNamespace(client_cache_bytes=1000),
        _timeline=SimpleNamespace(
            block_manifests=lambda: [], override_items=lambda: []
        ),
    )
    base.update(overrides)
    return cast(Any, SimpleNamespace(**base))


def test_client_session_uses_current_server_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    messages: list[Any] = []
    monkeypatch.setattr(ClientSession, "_send", lambda self, m: messages.append(m))
    server = _fake_session_server()
    session = ClientSession(server, cast(Any, SimpleNamespace(client_id=1)))

    messages.clear()
    session.play()
    assert isinstance(messages[-1], TimelinePlayMessage)
    assert messages[-1].speed == 2.0
    assert messages[-1].loop is True

    server.loop = False
    session.set_speed(0.5)
    assert isinstance(messages[-1], TimelineSetSpeedMessage)
    assert messages[-1].speed == 0.5
    assert messages[-1].loop is False


def test_client_session_syncs_existing_overrides_on_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = StoredMessage(
        {"type": "SetPositionMessage", "name": "/frame", "position": [1.0, 2.0, 3.0]}
    )
    puts, _ = scene_puts_deletes(stored)
    key, name, message = puts[0]
    entry = SceneEntryRecord(key, 1, name, message)

    messages: list[Any] = []
    monkeypatch.setattr(ClientSession, "_send", lambda self, m: messages.append(m))
    server = _fake_session_server(
        _timeline=SimpleNamespace(
            block_manifests=lambda: [], override_items=lambda: [entry]
        )
    )
    ClientSession(server, cast(Any, SimpleNamespace(client_id=1))).start()

    overrides = [m for m in messages if isinstance(m, TimelineOverrideMessage)]
    assert len(overrides) == 1
    assert overrides[0].key == key
    assert overrides[0].message["type"] == "SetPositionMessage"


def test_block_request_sends_fresh_manifests(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[Any] = []
    monkeypatch.setattr(ClientSession, "_send", lambda self, m: messages.append(m))
    manifests = [{"index": 0, "stepStart": 0, "stepStop": 2, "byteSize": 123}]
    server = _fake_session_server(
        _timeline=SimpleNamespace(
            block_manifests=lambda: manifests, override_items=lambda: []
        )
    )
    session = ClientSession(server, cast(Any, SimpleNamespace(client_id=1)))
    session._pending_requests.add(0)
    future: Future[Any] = Future()
    future.set_result(
        TimelineBlockMessage(index=0, checkpointScene=[], checkpointAudio=[], deltas=[])
    )

    session._finish_block_request(0, future)

    assert isinstance(messages[-2], TimelineBlockMessage)
    assert isinstance(messages[-1], TimelineManifestsMessage)
    assert messages[-1].manifests == manifests
    assert session.loaded_blocks == {0}


def test_live_scene_removals_forwarded_without_block_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = viser4d.Viser4dServer(num_steps=4, port=0, verbose=False)

    class FakeSession:
        def __init__(self) -> None:
            self.overrides: list[SceneEntryRecord] = []

        def apply_override(self, entry: SceneEntryRecord) -> None:
            self.overrides.append(entry)

    refresh_calls: list[int] = []
    monkeypatch.setattr(
        server._recorder,
        "_queue_block_refresh",
        lambda block: refresh_calls.append(block),
    )
    try:
        with server.at(0) as tl:
            frame = tl.scene.add_frame("/frame")
        fake = FakeSession()
        with server._sessions_lock:
            server._sessions[123] = cast(Any, fake)

        refresh_calls.clear()
        frame.remove()

        assert refresh_calls == []
        assert len(fake.overrides) == 1
        entry = fake.overrides[0]
        assert entry.key == "RemoveSceneNodeMessage:/frame"
        assert entry.message.payload["type"] == "RemoveSceneNodeMessage"
    finally:
        with server._sessions_lock:
            server._sessions.pop(123, None)
        server.stop()


def test_runtime_ready_before_attach_is_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)

    class FakeSession:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self.events: list[str] = []

        def start(self) -> None:
            pass

        def handle_event(self, message: object) -> None:
            self.events.append(type(message).__name__)

    monkeypatch.setattr(server_module, "ClientSession", FakeSession)
    try:
        server._handle_event(123, TimelineReadyMessage())
        attach = server._client_connect_cb[-1]
        attach(cast(Any, SimpleNamespace(client_id=123)))
        session = server.get_client_playback(123)
        assert isinstance(session, FakeSession)
        assert session.events == ["TimelineReadyMessage"]
        assert 123 not in server._pending_ready_ids
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Recording and serialization behavior
# ---------------------------------------------------------------------------


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


def test_at_keeps_server_scene_live() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            joint = tl.scene.add_frame("/joint")
            server.scene.add_frame("/static")
            joint.position = (2.0, 0.0, 0.0)
        recording = _deserialize_recording(server.serialize())
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
        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        positions = [
            tuple(cast(list[float], m["position"]))
            for _, m in messages
            if m.get("type") == "SetPositionMessage" and m.get("name") == "/joint"
        ]
        assert positions == [(2.0, 0.0, 0.0)]
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
        recording = _deserialize_recording(server.serialize())
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
        with pytest.raises(IndexError, match="out of range"):
            with server.at(3):
                pass
        with pytest.raises(ValueError, match="start_timestep must be in \\[0, 1\\]"):
            server.serialize(start_timestep=2, end_timestep=2)
    finally:
        server.stop()


def test_set_steps_rejects_active_recording() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with pytest.raises(
            RuntimeError, match="cannot run while inside server.at\\(t\\)"
        ):
            with server.at(0):
                server.set_steps(3)
    finally:
        server.stop()


def test_at_rejects_nested_sessions() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with pytest.raises(RuntimeError, match="cannot be nested"):
            with server.at(0):
                with server.at(1):
                    pass
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
        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        assert all(m.get("name") != "/joint" for _, m in messages)

        with server.at(1) as tl:
            tl.scene.add_frame("/joint")
        recording = _deserialize_recording(
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
        with pytest.raises(
            RuntimeError, match="cannot run while inside server.at\\(t\\)"
        ):
            with server.at(0):
                server.clear()
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


def test_at_rejects_static_name_collisions() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        server.scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))
        with pytest.raises(RuntimeError, match="static scene node"):
            with server.at(0) as tl:
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
        recording = _deserialize_recording(server.serialize())
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


def test_removed_static_nodes_serialize_as_removals() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        joint = server.scene.add_frame("/joint")
        joint.position = (1.0, 2.0, 3.0)
        joint.remove()
        recording = _deserialize_recording(server.serialize())
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
        recording = _deserialize_recording(base64.b64decode(match.group(1)))
        assert isinstance(recording["messages"], list)
    finally:
        server.stop()


def test_runtime_bootstrap_is_serialized_with_export() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        runtime = [m for _, m in messages if m.get("type") == "RunJavascriptMessage"]
        assert len(runtime) == 1
        assert str(runtime[0]["source"]).startswith(build_module.RUNTIME_MARKER)
    finally:
        server.stop()


def test_stop_unblocks_sleep_forever() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    sleeper = threading.Thread(target=server.sleep_forever)
    sleeper.start()
    try:
        time.sleep(0.05)
        server.stop()
        sleeper.join(timeout=1.0)
        assert sleeper.is_alive() is False
    finally:
        if sleeper.is_alive():
            server.stop()
            sleeper.join(timeout=1.0)


# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------


def test_audio_waveform_reflects_appended_data() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            audio = tl.audio.add_track(
                "/audio", data=np.array([1, 2], dtype=np.int16), sample_rate=16_000
            )
        with server.at(1):
            audio.append(np.array([3, 4], dtype=np.int16))
            audio.append(np.array([5, 6], dtype=np.int16))
        assert np.array_equal(
            audio.waveform, np.array([1, 2, 3, 4, 5, 6], dtype=np.int16)
        )
    finally:
        server.stop()


def test_audio_rejects_non_mono_or_stereo_shapes() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            with pytest.raises(ValueError, match="mono or stereo"):
                tl.audio.add_track(
                    "/audio",
                    data=np.zeros((2, 2, 2), dtype=np.float32),
                    sample_rate=16_000,
                )
            with pytest.raises(ValueError, match="mono or stereo"):
                tl.audio.add_track(
                    "/audio-3ch",
                    data=np.zeros((4, 3), dtype=np.float32),
                    sample_rate=16_000,
                )
    finally:
        server.stop()


def test_stereo_audio_append_preserves_layout() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            audio = tl.audio.add_track(
                "/audio",
                data=np.array([[1, 10], [2, 20]], dtype=np.int16),
                sample_rate=16_000,
            )
        with server.at(1):
            audio.append(np.array([[3, 30], [4, 40]], dtype=np.int16))
            expected = np.array([[1, 10], [2, 20], [3, 30], [4, 40]], dtype=np.int16)
            assert np.array_equal(audio.waveform, expected)
            with pytest.raises(ValueError, match="channel count"):
                audio.append(np.array([5, 6], dtype=np.int16))
    finally:
        server.stop()


def test_same_step_audio_events_serialize_without_deduping() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            audio = tl.audio.add_track(
                "/audio", data=np.array([1, 2], dtype=np.int16), sample_rate=16_000
            )
        with server.at(1):
            audio.append(np.array([3, 4], dtype=np.int16))
            audio.append(np.array([5, 6], dtype=np.int16))
        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        appends = [
            m
            for _, m in messages
            if m.get("type") == "AppendAudioMessage" and m.get("name") == "/audio"
        ]
        assert len(appends) == 2
    finally:
        server.stop()


def test_out_of_session_audio_edits_raise() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            audio = tl.audio.add_track(
                "/audio", data=np.array([1, 2], dtype=np.int16), sample_rate=16_000
            )
        with pytest.raises(RuntimeError, match="only valid inside server.at\\(t\\)"):
            audio.volume = 0.5
        assert audio.volume == 1.0

        with pytest.raises(RuntimeError, match="only valid inside server.at\\(t\\)"):
            audio.append(np.array([3, 4], dtype=np.int16))
        assert np.array_equal(audio.waveform, np.array([1, 2], dtype=np.int16))

        replacement = np.array([5, 6], dtype=np.int16)
        with pytest.raises(RuntimeError, match="only valid inside server.at\\(t\\)"):
            audio.waveform = replacement
        assert np.array_equal(audio.waveform, np.array([1, 2], dtype=np.int16))

        with pytest.raises(RuntimeError, match="only valid inside server.at\\(t\\)"):
            audio.remove()
    finally:
        server.stop()


def test_audio_rejects_non_positive_sample_rate() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            with pytest.raises(
                ValueError, match="sample_rate must be a positive integer"
            ):
                tl.audio.add_track(
                    "/audio", data=np.array([1, 2], dtype=np.int16), sample_rate=0
                )
    finally:
        server.stop()


def test_export_trims_preroll_audio() -> None:
    server = viser4d.Viser4dServer(num_steps=4, fps=2.0, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            tl.audio.add_track(
                "/audio", data=np.arange(8, dtype=np.float32), sample_rate=8
            )
            tl.audio.add_track(
                "/short", data=np.zeros(2, dtype=np.float32), sample_rate=8
            )

        recording = _deserialize_recording(server.serialize(start_timestep=1))
        messages = cast(list[tuple[float, dict[str, Any]]], recording["messages"])
        adds = [(t, m) for t, m in messages if m.get("type") == "AddAudioMessage"]

        # /short (0.25s) is fully elapsed by step 1 (0.5s); /audio is trimmed
        # by the elapsed 4 of its 8 frames and re-anchored at time 0.
        assert [m["name"] for _, m in adds] == ["/audio"]
        t, add = adds[0]
        assert t == 0.0
        assert add["waveform"]["numFrames"] == 4
        assert add["waveform"]["numChannels"] == 1
    finally:
        server.stop()


def test_audio_waveform_survives_checkpoint_fold() -> None:
    server = viser4d.Viser4dServer(
        num_steps=3,
        streaming=viser4d.StreamingConfig(block_size=1),
        port=0,
        verbose=False,
    )
    try:
        with server.at(0) as tl:
            audio = tl.audio.add_track(
                "/audio", data=np.array([1, 2], dtype=np.int16), sample_rate=8_000
            )
        with server.at(1):
            audio.append(np.array([3, 4], dtype=np.int16))
        message = server._timeline.block_message(2)
        assert len(message.checkpointAudio) == 1
        track = message.checkpointAudio[0]
        assert track["name"] == "/audio"
        assert track["startStep"] == 0
        assert track["sampleRate"] == 8_000
        assert track["waveform"]["numFrames"] == 4
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


def test_timeline_handle_updates_become_overrides() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            joint = tl.scene.add_frame("/joint")
        joint.position = (2.0, 0.0, 0.0)
        recording = _deserialize_recording(server.serialize())
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
        recording = _deserialize_recording(server.serialize())
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
        recording = _deserialize_recording(server.serialize())
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

        one_step = server._export._build(0, 0)
        all_steps = server._export._build(0, None)
        # The override applies at every step but its buffers land exactly once.
        assert len(_viser.serializer_binary_buffers(all_steps)) == len(
            _viser.serializer_binary_buffers(one_step)
        )
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
        recording = _deserialize_recording(server.serialize())
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


# ---------------------------------------------------------------------------
# Storage: disk spill + checkpoint invalidation
# ---------------------------------------------------------------------------


def test_serialization_survives_block_eviction_to_disk() -> None:
    server = viser4d.Viser4dServer(num_steps=300, fps=1.0, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            joint = tl.scene.add_frame("/joint")
        for step in (1, 64, 128, 192, 256):
            with server.at(step):
                joint.position = (float(step), 0.0, 0.0)
        recording = _deserialize_recording(server.serialize())
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


# ---------------------------------------------------------------------------
# Pure model unit tests
# ---------------------------------------------------------------------------


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
