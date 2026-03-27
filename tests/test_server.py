import asyncio
import threading
import time
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest
from viser import ScenePointerEvent, TransformControlsEvent, _messages
from viser.infra import ClientId

import viser4d
from viser4d.timeline import ClientPlaybackHandle


def _attach_fake_client(server: viser4d.Viser4dServer, client_id: int = 0) -> None:
    fake_scene = SimpleNamespace(
        _scene_pointer_cb=None,
        remove_pointer_callback=lambda: None,
    )
    fake_client = SimpleNamespace(
        client_id=client_id,
        scene=fake_scene,
        _websock_connection=SimpleNamespace(queue_message=lambda _message: None),
    )
    server._connected_clients = cast(  # type: ignore[assignment]
        dict[int, object],
        {client_id: fake_client},
    )


def _dispatch_viewer_message(
    server: viser4d.Viser4dServer,
    message: _messages.Message,
    client_id: int = 0,
) -> None:
    future = asyncio.run_coroutine_threadsafe(
        server._websock_server._handle_incoming_message(ClientId(client_id), message),
        server.get_event_loop(),
    )
    future.result(timeout=1.0)


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


def test_at_rejects_reusing_static_scene_names() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        server.scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))
        with pytest.raises(RuntimeError, match="static scene node with the same name"):
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


def test_timeline_click_callbacks_work_after_recording() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    clicked = threading.Event()

    try:
        with server.at(0) as timeline:
            joint = timeline.scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))

        _attach_fake_client(server)

        @joint.on_click
        async def _(_event: object) -> None:
            clicked.set()

        _dispatch_viewer_message(
            server,
            _messages.SceneNodeClickMessage(
                name="/joint",
                instance_index=None,
                ray_origin=(0.0, 0.0, 0.0),
                ray_direction=(0.0, 0.0, -1.0),
                screen_pos=(0.5, 0.5),
            ),
        )

        assert clicked.wait(timeout=1.0)
    finally:
        server.stop()


def test_timeline_handle_mutations_work_after_recording() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)

    try:
        with server.at(0) as timeline:
            joint = timeline.scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))

        joint.position = (1.0, 2.0, 3.0)
        joint.visible = False
        joint.remove()

        assert tuple(joint.position) == (1.0, 2.0, 3.0)
        assert joint.visible is False
    finally:
        server.stop()


def test_timeline_scene_pointer_callbacks_work_after_recording() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    clicked = threading.Event()

    try:
        with server.at(0) as timeline:
            timeline_scene = timeline.scene
            timeline_scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))

        _attach_fake_client(server)

        @timeline_scene.on_pointer_event("click")
        def _(_event: ScenePointerEvent) -> None:
            clicked.set()

        _dispatch_viewer_message(
            server,
            _messages.ScenePointerMessage(
                event_type="click",
                ray_origin=(0.0, 0.0, 0.0),
                ray_direction=(0.0, 0.0, -1.0),
                screen_pos=((0.5, 0.5),),
            ),
        )

        assert clicked.wait(timeout=1.0)
    finally:
        server.stop()


def test_timeline_transform_controls_callbacks_work_after_recording() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    updates: list[tuple[int | None, tuple[float, float, float]]] = []
    updated = threading.Event()

    try:
        with server.at(0) as timeline:
            controls = timeline.scene.add_transform_controls("/joint")

        _attach_fake_client(server)

        @controls.on_update
        def _(event: TransformControlsEvent) -> None:
            position = cast(tuple[float, float, float], tuple(event.target.position))
            updates.append((event.client_id, position))
            updated.set()

        _dispatch_viewer_message(
            server,
            _messages.TransformControlsUpdateMessage(
                name="/joint",
                wxyz=(1.0, 0.0, 0.0, 0.0),
                position=(1.0, 2.0, 3.0),
            ),
        )

        assert updated.wait(timeout=1.0)
        assert updates == [(0, (1.0, 2.0, 3.0))]
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
