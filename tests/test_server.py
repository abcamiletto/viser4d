import base64
import re
import threading
import time
from types import SimpleNamespace
from typing import Any, cast

import msgspec
import numpy as np
import pytest
import zstandard

import viser4d
from viser4d import _server as server_module
from viser4d import _runtime as runtime_module
from viser4d._types import StoredMessage
from viser4d._runtime_messages import (
    RuntimePlayMessage,
    RuntimeReadyMessage,
    RuntimeSetSpeedMessage,
)
from viser4d.timeline._playback import ClientPlaybackHandle


def _deserialize_recording(blob: bytes) -> dict[str, object]:
    inner_size = int.from_bytes(blob[:8], "little")
    inner = zstandard.ZstdDecompressor().decompress(
        blob[8:], max_output_size=inner_size
    )
    assert len(inner) == inner_size
    msgpack_size = int.from_bytes(inner[:8], "little")
    return cast(dict[str, object], msgspec.msgpack.decode(inner[8 : 8 + msgpack_size]))


def _fake_create_gui(
    self: ClientPlaybackHandle,
    _brand_color: tuple[int, int, int] | None,
) -> None:
    self._timeline_slider = SimpleNamespace(value=0, max=1)
    self._speed_slider = SimpleNamespace(value=self._speed)
    self._step_buttons = SimpleNamespace()
    self._play_button = SimpleNamespace()
    self._pause_button = SimpleNamespace()


class _FakeLoadedPlayback:
    def __init__(self, *loaded_blocks: int) -> None:
        self.loaded_blocks = set(loaded_blocks)
        self.loaded_payloads: list[dict[str, object]] = []
        self.patched_payloads: list[dict[str, object]] = []

    def load_block(self, payload: dict[str, object]) -> None:
        self.loaded_payloads.append(payload)

    def patch_block(self, payload: dict[str, object]) -> None:
        self.patched_payloads.append(payload)


def _stored_message_types(messages: object) -> list[str]:
    return [
        str(message.payload["type"])
        for message in cast(list[StoredMessage], messages)
    ]


def test_server_does_not_expose_audio_api() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        assert hasattr(server, "audio") is False
    finally:
        server.stop()


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
        viser4d.Viser4dServer(
            num_steps=1,
            playback_speed=0.0,
            port=0,
            verbose=False,
        )

    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with pytest.raises(ValueError, match="speed must be a positive finite float"):
            server.set_playback_speed(-1.0)
    finally:
        server.stop()


def test_client_chunk_cache_size_comes_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISER4D_CLIENT_CHUNK_CACHE_SIZE", "2MB")
    server = viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)
    try:
        assert server.client_chunk_cache_bytes == 2_000_000
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
        ValueError,
        match="VISER4D_BLOCK_SIZE must be a positive integer",
    ):
        viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)


def test_client_chunk_cache_size_rejects_invalid_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISER4D_CLIENT_CHUNK_CACHE_SIZE", "invalid")
    with pytest.raises(
        ValueError,
        match="VISER4D_CLIENT_CHUNK_CACHE_SIZE must be an integer byte count",
    ):
        viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)


def test_chunk_streaming_config_is_public_and_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISER4D_BLOCK_SIZE", "16")
    monkeypatch.setenv("VISER4D_CLIENT_CHUNK_CACHE_SIZE", "2MB")
    config = viser4d.ChunkStreamingConfig(
        block_size=8,
        client_chunk_cache_bytes=1234,
    )
    server = viser4d.Viser4dServer(
        num_steps=100,
        chunk_streaming=config,
        port=0,
        verbose=False,
    )
    try:
        assert server.chunk_streaming == config
        assert server.block_size == 8
        assert server.client_chunk_cache_bytes == 1234
    finally:
        server.stop()


def test_server_uses_32_step_chunks_by_default() -> None:
    server = viser4d.Viser4dServer(num_steps=100, port=0, verbose=False)
    try:
        assert server.block_size == 32
    finally:
        server.stop()


def test_play_no_longer_accepts_loop_or_speed_keywords() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    untyped_server = cast(Any, server)
    try:
        with pytest.raises(TypeError, match="unexpected keyword argument 'loop'"):
            untyped_server.play(loop=True)
        with pytest.raises(TypeError, match="unexpected keyword argument 'speed'"):
            untyped_server.play(speed=2.0)
    finally:
        server.stop()


def test_server_playback_configuration_propagates_to_connected_and_future_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = viser4d.Viser4dServer(
        num_steps=2,
        loop=True,
        playback_speed=1.5,
        port=0,
        verbose=False,
    )

    class FakePlayback:
        def __init__(self, server: Any, client: Any, **_kwargs) -> None:
            self.client_id = client.client_id
            self.loop_on_init = server.loop
            self.speed_on_init = server.playback_speed
            self.config_syncs = 0
            self.play_calls = 0

        def play(self) -> None:
            self.play_calls += 1

        def sync_runtime_config(self) -> None:
            self.config_syncs += 1

        def set_speed(self, speed: float) -> None:
            self.speed_on_init = speed

        def handle_runtime_event(self, _message: object) -> None:
            pass

    monkeypatch.setattr(server_module, "ClientPlaybackHandle", FakePlayback)

    try:
        attach_playback = server._client_connect_cb[-1]
        attach_playback(cast(Any, SimpleNamespace(client_id=123)))
        first = server.get_client_playback(123)

        assert isinstance(first, FakePlayback)
        assert first.loop_on_init is True
        assert first.speed_on_init == 1.5
        assert server.loop is True
        assert server.playback_speed == 1.5

        server.set_loop(False)
        server.set_playback_speed(0.5)

        assert first.config_syncs == 1
        assert first.speed_on_init == 0.5
        assert server.loop is False
        assert server.playback_speed == 0.5

        attach_playback(cast(Any, SimpleNamespace(client_id=456)))
        second = server.get_client_playback(456)

        assert isinstance(second, FakePlayback)
        assert second.loop_on_init is False
        assert second.speed_on_init == 0.5

        server.play()

        assert first.play_calls == 1
        assert second.play_calls == 1
    finally:
        server.stop()


def test_timeline_operations_serialize_and_playback_commands() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            frame = timeline.scene.add_frame("/frame")
            audio = timeline.audio.add_track(
                "/audio",
                data=np.array([0, 1, 2], dtype=np.int16),
                sample_rate=16_000,
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


def test_client_playback_uses_current_server_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ClientPlaybackHandle, "_create_gui", _fake_create_gui)
    monkeypatch.setattr(ClientPlaybackHandle, "sync_runtime_config", lambda self: None)
    monkeypatch.setattr(
        ClientPlaybackHandle,
        "_sync_loaded_blocks",
        lambda self, timestep, force=False: None,
    )
    monkeypatch.setattr(
        ClientPlaybackHandle,
        "_send_runtime_message",
        lambda self, message: messages.append(message),
    )

    server = cast(
        Any,
        SimpleNamespace(loop=True, playback_speed=2.0, num_steps=2, fps=1.0),
    )
    client = cast(Any, SimpleNamespace(gui=None))
    messages: list[Any] = []
    playback = ClientPlaybackHandle(server, client)

    messages.clear()
    playback.play()

    assert isinstance(messages[-1], RuntimePlayMessage)
    assert messages[-1].speed == 2.0
    assert messages[-1].loop is True

    server.loop = False
    playback.set_speed(0.5)

    assert isinstance(messages[-1], RuntimeSetSpeedMessage)
    assert messages[-1].speed == 0.5
    assert messages[-1].loop is False


def test_at_keeps_server_scene_live() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")
            server.scene.add_frame("/static")
            joint.position = (2.0, 0.0, 0.0)

        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        creation_times = [
            time
            for time, message in messages
            if message.get("type") == "FrameMessage" and message.get("name") == "/joint"
        ]
        positions = [
            tuple(cast(list[float], message["position"]))
            for _, message in messages
            if message.get("type") == "SetPositionMessage"
            and message.get("name") == "/joint"
        ]
        static_times = [
            time
            for time, message in messages
            if message.get("type") == "FrameMessage"
            and message.get("name") == "/static"
        ]

        assert creation_times == [0.0]
        assert positions == [(2.0, 0.0, 0.0)]
        assert static_times == [0.0]
    finally:
        server.stop()


def test_same_step_scene_updates_serialize_latest_value_once() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")
        with server.at(0):
            joint.position = (1.0, 0.0, 0.0)
        with server.at(0):
            joint.position = (2.0, 0.0, 0.0)

        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        positions = [
            tuple(cast(list[float], message["position"]))
            for _, message in messages
            if message.get("type") == "SetPositionMessage"
            and message.get("name") == "/joint"
        ]

        assert positions == [(2.0, 0.0, 0.0)]
    finally:
        server.stop()


def test_set_steps_can_grow_timeline() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(1) as timeline:
            joint = timeline.scene.add_frame("/joint")

        server.set_steps(4)

        with server.at(3):
            joint.position = (3.0, 0.0, 0.0)

        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        positions = [
            tuple(cast(list[float], message["position"]))
            for _, message in messages
            if message.get("type") == "SetPositionMessage"
            and message.get("name") == "/joint"
        ]

        assert positions == [(3.0, 0.0, 0.0)]
    finally:
        server.stop()


def test_set_steps_can_shrink_timeline() -> None:
    server = viser4d.Viser4dServer(num_steps=4, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")

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


def test_set_steps_rejects_active_timeline_recording() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with pytest.raises(
            RuntimeError, match="cannot run while inside server.at\\(t\\)"
        ):
            with server.at(0):
                server.set_steps(3)
    finally:
        server.stop()


def test_at_rejects_nested_recording_sessions() -> None:
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
        with server.at(0) as timeline:
            timeline.scene.add_frame("/joint")

        server.clear()

        assert server.scene.get_handle_by_name("/static") is None

        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        assert all(message.get("name") != "/joint" for _, message in messages)

        with server.at(1) as timeline:
            timeline.scene.add_frame("/joint")

        recording = _deserialize_recording(
            server.serialize(start_timestep=1, end_timestep=1)
        )
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        creation_messages = [
            message
            for _, message in messages
            if message.get("type") == "FrameMessage" and message.get("name") == "/joint"
        ]

        assert len(creation_messages) == 1
    finally:
        server.stop()


def test_clear_rejects_active_timeline_recording() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with pytest.raises(
            RuntimeError, match="cannot run while inside server.at\\(t\\)"
        ):
            with server.at(0):
                server.clear()
    finally:
        server.stop()


def test_serialize_rejects_invalid_timestep_range() -> None:
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
            with server.at(0) as timeline:
                timeline.scene.add_icosphere("/joint", position=(1.0, 0.0, 0.0))
    finally:
        server.stop()


def test_at_allows_recreating_timeline_nodes() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            timeline.scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))

        with server.at(1) as timeline:
            joint = timeline.scene.add_icosphere("/joint", position=(1.0, 0.0, 0.0))
            joint.position = (2.0, 0.0, 0.0)

        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        creation_times = [
            time
            for time, message in messages
            if message.get("type") == "IcosphereMessage"
            and message.get("name") == "/joint"
        ]
        positions = [
            (time, tuple(cast(list[float], message["position"])))
            for time, message in messages
            if message.get("type") == "SetPositionMessage"
            and message.get("name") == "/joint"
        ]

        assert creation_times == [0.0, 1.0 / server.fps]
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
        joint_messages = [
            message for _, message in messages if message.get("name") == "/joint"
        ]

        assert joint_messages[-1]["type"] == "RemoveSceneNodeMessage"
    finally:
        server.stop()


def test_as_html_embeds_native_viser_recording_for_viewer() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            timeline.scene.add_frame("/joint")

        html = server.as_html()
        match = re.search(r'window\.__VISER_EMBED_DATA__="([^"]+)"', html)
        assert match is not None
        embed_bytes = base64.b64decode(match.group(1))
        recording = _deserialize_recording(embed_bytes)
        assert isinstance(recording["messages"], list)
    finally:
        server.stop()


def test_runtime_bootstrap_is_serialized_with_export() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        runtime_messages = [
            message
            for _, message in messages
            if message.get("type") == "RunJavascriptMessage"
        ]

        assert len(runtime_messages) == 1
        assert str(runtime_messages[0]["source"]).startswith(
            runtime_module.RUNTIME_MARKER
        )
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


def test_audio_waveform_reflects_appended_data() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            audio = timeline.audio.add_track(
                "/audio",
                data=np.array([1, 2], dtype=np.int16),
                sample_rate=16_000,
            )

        audio.append(np.array([3, 4], dtype=np.int16))
        audio.append(np.array([5, 6], dtype=np.int16))

        expected = np.array([1, 2, 3, 4, 5, 6], dtype=np.int16)
        assert np.array_equal(audio.waveform, expected)
    finally:
        server.stop()


def test_audio_rejects_non_mono_or_stereo_shapes() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            with pytest.raises(ValueError, match="mono or stereo"):
                timeline.audio.add_track(
                    "/audio",
                    data=np.zeros((2, 2, 2), dtype=np.float32),
                    sample_rate=16_000,
                )
            with pytest.raises(ValueError, match="mono or stereo"):
                timeline.audio.add_track(
                    "/audio-3ch",
                    data=np.zeros((4, 3), dtype=np.float32),
                    sample_rate=16_000,
                )
    finally:
        server.stop()


def test_stereo_audio_append_preserves_channel_layout() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            audio = timeline.audio.add_track(
                "/audio",
                data=np.array([[1, 10], [2, 20]], dtype=np.int16),
                sample_rate=16_000,
            )

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
        with server.at(0) as timeline:
            audio = timeline.audio.add_track(
                "/audio",
                data=np.array([1, 2], dtype=np.int16),
                sample_rate=16_000,
            )

        with server.at(1):
            audio.append(np.array([3, 4], dtype=np.int16))
            audio.append(np.array([5, 6], dtype=np.int16))

        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        append_messages = [
            message
            for _, message in messages
            if message.get("type") == "AppendAudioMessage"
            and message.get("name") == "/audio"
        ]

        assert len(append_messages) == 2
    finally:
        server.stop()


def test_timeline_handle_updates_become_global_overrides() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")

        joint.position = (2.0, 0.0, 0.0)

        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        positions = [
            (time, tuple(cast(list[float], message["position"])))
            for time, message in messages
            if message.get("type") == "SetPositionMessage"
            and message.get("name") == "/joint"
        ]

        assert positions == [
            (0.0, (2.0, 0.0, 0.0)),
            (1.0 / server.fps, (2.0, 0.0, 0.0)),
        ]
    finally:
        server.stop()


def test_timeline_global_overrides_reapply_after_recorded_updates() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")

        with server.at(1):
            joint.position = (1.0, 0.0, 0.0)

        joint.position = (2.0, 0.0, 0.0)

        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        positions = [
            (time, tuple(cast(list[float], message["position"])))
            for time, message in messages
            if message.get("type") == "SetPositionMessage"
            and message.get("name") == "/joint"
        ]

        assert positions == [
            (0.0, (2.0, 0.0, 0.0)),
            (1.0 / server.fps, (1.0, 0.0, 0.0)),
            (1.0 / server.fps, (2.0, 0.0, 0.0)),
        ]
    finally:
        server.stop()


def test_recorded_step_updates_patch_loaded_blocks() -> None:
    server = viser4d.Viser4dServer(num_steps=65, fps=1.0, port=0, verbose=False)
    try:
        server._recorder._CLIENT_REFRESH_DELAY_SECONDS = 60.0
        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")
        server._recorder._cancel_pending_refresh()

        playback = _FakeLoadedPlayback(0, 1)
        server._client_playbacks = {1: cast(Any, playback)}

        with server.at(1):
            joint.visible = False
        server._recorder._flush_client_block_refreshes()

        assert playback.loaded_payloads == []
        assert [payload["block"] for payload in playback.patched_payloads] == [0, 1]

        current_block, future_block = playback.patched_payloads
        assert current_block["checkpointMessages"] is None
        current_step_deltas = cast(
            list[dict[str, object]],
            current_block["stepDeltas"],
        )
        assert len(current_step_deltas) == 1
        assert current_step_deltas[0]["offset"] == 1
        assert _stored_message_types(current_step_deltas[0]["messages"]) == [
            "SetSceneNodeVisibilityMessage"
        ]

        assert future_block["checkpointMessages"] is not None
        assert future_block["stepDeltas"] == []
        assert _stored_message_types(future_block["checkpointMessages"]) == [
            "FrameMessage",
            "SetSceneNodeVisibilityMessage",
        ]
    finally:
        server.stop()


def test_global_override_updates_fall_back_to_full_block_reload() -> None:
    server = viser4d.Viser4dServer(num_steps=65, fps=1.0, port=0, verbose=False)
    try:
        server._recorder._CLIENT_REFRESH_DELAY_SECONDS = 60.0
        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")
        server._recorder._cancel_pending_refresh()

        playback = _FakeLoadedPlayback(0, 1)
        server._client_playbacks = {1: cast(Any, playback)}

        joint.visible = False
        server._recorder._flush_client_block_refreshes()

        assert playback.patched_payloads == []
        assert [payload["block"] for payload in playback.loaded_payloads] == [0, 1]
    finally:
        server.stop()


def test_timeline_scene_creation_still_requires_timestep_context() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            scene = timeline.scene

        with pytest.raises(
            RuntimeError, match="creation is only valid inside server.at\\(t\\)"
        ):
            scene.add_frame("/joint")
    finally:
        server.stop()


def test_late_created_timeline_nodes_keep_their_creation_step() -> None:
    server = viser4d.Viser4dServer(num_steps=6, fps=1.0, port=0, verbose=False)
    try:
        with server.at(5) as timeline:
            joint = timeline.scene.add_frame("/joint")
            joint.position = (2.0, 0.0, 0.0)

        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])

        creation_times = [
            time
            for time, message in messages
            if message.get("type") == "FrameMessage" and message.get("name") == "/joint"
        ]
        position_times = [
            time
            for time, message in messages
            if message.get("type") == "SetPositionMessage"
            and message.get("name") == "/joint"
        ]

        assert creation_times == [5.0]
        assert position_times == [5.0]
    finally:
        server.stop()


def test_serialization_survives_block_eviction_to_disk() -> None:
    server = viser4d.Viser4dServer(num_steps=300, fps=1.0, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")

        for step in (1, 64, 128, 192, 256):
            with server.at(step):
                joint.position = (float(step), 0.0, 0.0)

        recording = _deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        position_times = [
            time
            for time, message in messages
            if message.get("type") == "SetPositionMessage"
            and message.get("name") == "/joint"
        ]
        positions = [
            tuple(cast(list[float], message["position"]))
            for _, message in messages
            if message.get("type") == "SetPositionMessage"
            and message.get("name") == "/joint"
        ]

        assert position_times == [1.0, 64.0, 128.0, 192.0, 256.0]
        assert positions == [
            (1.0, 0.0, 0.0),
            (64.0, 0.0, 0.0),
            (128.0, 0.0, 0.0),
            (192.0, 0.0, 0.0),
            (256.0, 0.0, 0.0),
        ]
    finally:
        server.stop()


def test_runtime_ready_before_playback_attach_is_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)

    class FakePlayback:
        def __init__(self, *_args, **_kwargs) -> None:
            self.events: list[str] = []

        def handle_runtime_event(self, message: object) -> None:
            self.events.append(type(message).__name__)

    monkeypatch.setattr(server_module, "ClientPlaybackHandle", FakePlayback)

    try:
        server._handle_runtime_event(123, RuntimeReadyMessage())
        attach_playback = server._client_connect_cb[-1]
        attach_playback(cast(Any, SimpleNamespace(client_id=123)))

        playback = server.get_client_playback(123)

        assert isinstance(playback, FakePlayback)
        assert playback.events == ["RuntimeReadyMessage"]
        assert 123 not in server._pending_runtime_ready_client_ids
    finally:
        server.stop()
