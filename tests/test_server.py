import threading
import time
from types import SimpleNamespace
from typing import cast

import msgspec
import numpy as np
import pytest
import zstandard

import viser4d
from viser4d.timeline import ClientPlaybackHandle


def _deserialize_recording(blob: bytes) -> dict[str, object]:
    packed_size = int.from_bytes(blob[:8], "little")
    packed = zstandard.ZstdDecompressor().decompress(
        blob[8:], max_output_size=packed_size
    )
    return cast(dict[str, object], msgspec.msgpack.decode(packed))


def test_audio_requires_timestep_context() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with pytest.raises(RuntimeError):
            server.audio.add_track(
                "/audio", data=np.zeros(4, dtype=np.int16), sample_rate=8_000
            )
    finally:
        server.stop()


def test_num_steps_must_be_positive() -> None:
    with pytest.raises(ValueError, match="num_steps must be >= 1"):
        viser4d.Viser4dServer(num_steps=0, port=0, verbose=False)


def test_fps_and_speed_must_be_positive() -> None:
    with pytest.raises(ValueError, match="fps must be a positive finite float"):
        viser4d.Viser4dServer(num_steps=1, fps=0.0, port=0, verbose=False)

    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with pytest.raises(ValueError, match="speed must be a positive finite float"):
            server.play(speed=0.0)
        with pytest.raises(ValueError, match="speed must be a positive finite float"):
            server.set_playback_speed(-1.0)
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


def test_at_preserves_server_scene_backwards_compatibility() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0):
            joint = server.scene.add_frame("/joint")
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


def test_serialize_rejects_invalid_timestep_range() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with pytest.raises(
            ValueError, match="start_timestep must be less than or equal"
        ):
            server.serialize(start_timestep=2, end_timestep=1)
    finally:
        server.stop()


def test_timestep_change_callbacks_follow_client_events() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    seen_events: list[tuple[int, int]] = []
    client = SimpleNamespace(client_id=7)

    try:

        def _on_timestep(client_handle: object, timestep: int) -> None:
            seen_events.append((getattr(client_handle, "client_id"), timestep))

        server.on_timestep_change(_on_timestep)
        server._dispatch_timestep_change(client, 2)  # type: ignore[arg-type]

        assert seen_events == [(7, 2)]
    finally:
        server.stop()


def test_playback_change_callbacks_follow_client_events() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    seen_events: list[tuple[int, bool]] = []
    client = SimpleNamespace(client_id=7)

    try:

        def _on_playback(client_handle: object, is_playing: bool) -> None:
            seen_events.append((getattr(client_handle, "client_id"), is_playing))

        server.on_playback_change(_on_playback)
        server._dispatch_playback_change(client, True)  # type: ignore[arg-type]

        assert seen_events == [(7, True)]
    finally:
        server.stop()


def test_client_playback_sync_dispatches_server_callbacks() -> None:
    seen_timesteps: list[tuple[int, int]] = []
    seen_playback: list[tuple[int, bool]] = []

    class _DummyServer:
        num_steps = 4
        fps = 30.0

        def _dispatch_timestep_change(self, client: object, timestep: int) -> None:
            seen_timesteps.append((getattr(client, "client_id"), timestep))

        def _dispatch_playback_change(self, client: object, is_playing: bool) -> None:
            seen_playback.append((getattr(client, "client_id"), is_playing))

    playback = ClientPlaybackHandle.__new__(ClientPlaybackHandle)
    playback._server = _DummyServer()
    playback._client = SimpleNamespace(client_id=11)
    playback._lock = threading.RLock()
    playback._current_timestep = 0
    playback._speed = 1.0
    playback._is_playing = False
    playback._loop = False

    playback._sync_speed_from_client(1.5)
    playback._sync_playback_from_client(True)
    playback._sync_playback_from_client(True)
    playback._sync_from_client(2)
    playback._sync_playback_from_client(False)

    assert playback.current_timestep == 2
    assert playback.speed == 1.5
    assert playback._server.fps * playback.speed == 45.0
    assert playback.is_playing is False
    assert seen_timesteps == [(11, 2)]
    assert seen_playback == [(11, True), (11, False)]


def test_playback_state_tracks_browser_reports_not_commands() -> None:
    sent_calls: list[tuple[str, object]] = []
    seen_playback: list[tuple[int, bool]] = []

    class _DummyServer:
        fps = 30.0

        def _dispatch_playback_change(self, client: object, is_playing: bool) -> None:
            seen_playback.append((getattr(client, "client_id"), is_playing))

    playback = ClientPlaybackHandle.__new__(ClientPlaybackHandle)
    playback._server = _DummyServer()
    playback._client = SimpleNamespace(client_id=11)
    playback._lock = threading.RLock()
    playback._speed = 1.0
    playback._loop = False
    playback._is_playing = False
    playback._set_speed_slider_value = lambda speed: None
    playback._send_runtime_call = lambda method, payload: sent_calls.append(
        (method, payload)
    )

    playback.play()
    assert playback.is_playing is False

    playback._sync_speed_from_client(0.5)
    assert playback.speed == 0.5
    assert playback._server.fps * playback.speed == 15.0

    playback._sync_playback_from_client(True)
    assert playback.is_playing is True

    playback.pause()
    assert playback.is_playing is True

    playback._sync_playback_from_client(False)
    assert playback.is_playing is False
    assert sent_calls == [
        ("play", {"speed": 1.0, "loop": False}),
        ("pause", {}),
    ]
    assert seen_playback == [(11, True), (11, False)]


def test_server_broadcast_commands_only_touch_connected_clients() -> None:
    server = viser4d.Viser4dServer(num_steps=4, port=0, verbose=False)

    class _PlaybackStub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def play(self, speed: float | None = None, loop: bool = False) -> None:
            self.calls.append(("play", (speed, loop)))

        def pause(self) -> None:
            self.calls.append(("pause", None))

        def refresh(self) -> None:
            self.calls.append(("refresh", None))

        def set_speed(self, speed: float) -> None:
            self.calls.append(("set_speed", speed))

    try:
        first = _PlaybackStub()
        second = _PlaybackStub()
        server._client_playbacks = cast(
            dict[int, ClientPlaybackHandle],
            {1: first, 2: second},
        )

        assert server.fps == 30.0
        assert server._timeline_fps == 30.0
        server.play(speed=0.5, loop=True)
        assert server.fps == 30.0
        assert server._timeline_fps == 30.0
        server.pause()
        server.play()
        server.set_playback_speed(2.0)
        assert server.fps == 30.0
        assert server._timeline_fps == 30.0
        server.pause()
        server.refresh()

        expected = [
            ("play", (0.5, True)),
            ("pause", None),
            ("play", (None, False)),
            ("set_speed", 2.0),
            ("pause", None),
            ("refresh", None),
        ]
        assert first.calls == expected
        assert second.calls == expected
    finally:
        server.stop()


def test_server_exposes_client_playbacks() -> None:
    server = viser4d.Viser4dServer(num_steps=4, port=0, verbose=False)

    try:
        first = cast(ClientPlaybackHandle, SimpleNamespace(is_playing=True))
        second = cast(ClientPlaybackHandle, SimpleNamespace(is_playing=False))
        server._client_playbacks = {1: first, 2: second}

        assert server.get_client_playback(1) is first
        assert server.get_client_playback(2) is second
        assert server.get_client_playback(3) is None

        playbacks = server.get_client_playbacks()

        assert playbacks == {1: first, 2: second}
        assert playbacks is not server._client_playbacks
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


def test_at_rejects_recreating_timeline_nodes() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            timeline.scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))

        with pytest.raises(RuntimeError, match="Cannot create timeline node"):
            with server.at(1) as timeline:
                timeline.scene.add_icosphere("/joint", position=(1.0, 0.0, 0.0))
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


def test_post_recording_timeline_updates_persist_in_serialization() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as timeline:
            joint = timeline.scene.add_frame("/joint")

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
