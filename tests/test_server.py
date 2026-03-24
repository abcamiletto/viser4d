import threading
import time
from types import SimpleNamespace
from typing import cast

import numpy as np
import pytest

import viser4d
from viser4d.timeline import ClientPlaybackHandle


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


def test_timeline_operations_serialize_and_playback_commands() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with server.at(0):
            frame = server.scene.add_frame("/frame")
            audio = server.audio.add_track(
                "/audio",
                data=np.array([0, 1, 2], dtype=np.int16),
                sample_rate=16_000,
            )
        with server.at(1):
            frame.position = (1.0, 2.0, 3.0)
            audio.volume = 0.25

        server.set_fps(24.0)
        server.play()
        server.pause()

        assert server.serialize()
        assert server.serialize(start_timestep=1, end_timestep=1)
    finally:
        server.stop()


def test_serialize_rejects_invalid_timestep_range() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with pytest.raises(AssertionError):
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

        def _dispatch_timestep_change(self, client: object, timestep: int) -> None:
            seen_timesteps.append((getattr(client, "client_id"), timestep))

        def _dispatch_playback_change(self, client: object, is_playing: bool) -> None:
            seen_playback.append((getattr(client, "client_id"), is_playing))

    playback = ClientPlaybackHandle.__new__(ClientPlaybackHandle)
    playback._server = _DummyServer()
    playback._client = SimpleNamespace(client_id=11)
    playback._lock = threading.RLock()
    playback._current_timestep = 0
    playback._is_playing = False
    playback._loop = False

    playback._sync_playback_from_client(True)
    playback._sync_playback_from_client(True)
    playback._sync_from_client(2)
    playback._sync_playback_from_client(False)

    assert playback.current_timestep == 2
    assert playback.is_playing is False
    assert seen_timesteps == [(11, 2)]
    assert seen_playback == [(11, True), (11, False)]


def test_playback_state_tracks_browser_reports_not_commands() -> None:
    sent_calls: list[tuple[str, object]] = []
    seen_playback: list[tuple[int, bool]] = []

    class _DummyServer:
        def _dispatch_playback_change(self, client: object, is_playing: bool) -> None:
            seen_playback.append((getattr(client, "client_id"), is_playing))

    playback = ClientPlaybackHandle.__new__(ClientPlaybackHandle)
    playback._server = _DummyServer()
    playback._client = SimpleNamespace(client_id=11)
    playback._lock = threading.RLock()
    playback._fps = 30.0
    playback._loop = False
    playback._is_playing = False
    playback._set_fps_slider_value = lambda fps: None
    playback._send_runtime_call = lambda method, payload: sent_calls.append(
        (method, payload)
    )

    playback.play()
    assert playback.is_playing is False

    playback._sync_playback_from_client(True)
    assert playback.is_playing is True

    playback.pause()
    assert playback.is_playing is True

    playback._sync_playback_from_client(False)
    assert playback.is_playing is False
    assert sent_calls == [
        ("play", {"fps": 30.0, "loop": False}),
        ("pause", {}),
    ]
    assert seen_playback == [(11, True), (11, False)]


def test_server_broadcast_commands_only_touch_connected_clients() -> None:
    server = viser4d.Viser4dServer(num_steps=4, port=0, verbose=False)

    class _PlaybackStub:
        def __init__(self) -> None:
            self.calls: list[tuple[str, object]] = []

        def play(self, fps: float, loop: bool = False) -> None:
            self.calls.append(("play", (fps, loop)))

        def pause(self) -> None:
            self.calls.append(("pause", None))

        def refresh(self) -> None:
            self.calls.append(("refresh", None))

        def set_fps(self, fps: float) -> None:
            self.calls.append(("set_fps", fps))

    try:
        first = _PlaybackStub()
        second = _PlaybackStub()
        server._client_playbacks = cast(
            dict[int, ClientPlaybackHandle],
            {1: first, 2: second},
        )

        assert server.fps == 30.0
        assert server._timeline_fps == 30.0
        server.play(fps=12.0, loop=True)
        assert server.fps == 12.0
        assert server._timeline_fps == 30.0
        server.pause()
        server.play()
        server.set_fps(24.0)
        assert server.fps == 24.0
        assert server._timeline_fps == 30.0
        server.pause()
        server.refresh()

        expected = [
            ("play", (12.0, True)),
            ("pause", None),
            ("play", (12.0, False)),
            ("set_fps", 24.0),
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

        playbacks = server.get_client_playbacks()

        assert playbacks == {1: first, 2: second}
        assert playbacks is not server._client_playbacks
    finally:
        server.stop()


def test_at_rejects_updates_to_static_scene_nodes() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        joint = server.scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))
        with pytest.raises(RuntimeError, match="Cannot modify static scene node"):
            with server.at(0):
                joint.position = (1.0, 0.0, 0.0)
    finally:
        server.stop()


def test_at_rejects_recreating_timeline_nodes() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0):
            server.scene.add_icosphere("/joint", position=(0.0, 0.0, 0.0))

        with pytest.raises(RuntimeError, match="Cannot create timeline node"):
            with server.at(1):
                server.scene.add_icosphere("/joint", position=(1.0, 0.0, 0.0))
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
        with server.at(0):
            audio = server.audio.add_track(
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
        with server.at(0):
            with pytest.raises(ValueError, match="mono or stereo"):
                server.audio.add_track(
                    "/audio",
                    data=np.zeros((2, 2, 2), dtype=np.float32),
                    sample_rate=16_000,
                )
            with pytest.raises(ValueError, match="mono or stereo"):
                server.audio.add_track(
                    "/audio-3ch",
                    data=np.zeros((4, 3), dtype=np.float32),
                    sample_rate=16_000,
                )
    finally:
        server.stop()


def test_stereo_audio_append_preserves_channel_layout() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0):
            audio = server.audio.add_track(
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
