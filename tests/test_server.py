import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

import viser4d
from viser4d.timeline import ClientPlaybackHandle, TimelineController


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


def test_timeline_operations_serialize_and_seek() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        seen_timesteps: list[int] = []
        server.on_timestep_change(seen_timesteps.append)

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
        server.seek(2)

        assert seen_timesteps[-1] == 2
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


def test_current_timestep_is_public() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        assert server.current_timestep == 0
        server.seek(2)
        assert server.current_timestep == 2
    finally:
        server.stop()


def test_on_client_timestep_change_dispatches_client_and_step() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    callback_called = threading.Event()
    seen: list[tuple[int, int]] = []
    client = SimpleNamespace(client_id=7)

    try:
        def _on_client_timestep(client_handle: object, timestep: int) -> None:
            seen.append((getattr(client_handle, "client_id"), timestep))
            callback_called.set()

        server.on_client_timestep_change(_on_client_timestep)
        server._dispatch_client_timestep_change(client, 2)  # type: ignore[arg-type]

        assert callback_called.wait(timeout=1.0)
        assert seen == [(7, 2)]
    finally:
        server.stop()


def test_client_playback_sync_dispatches_server_client_timestep_callback() -> None:
    seen: list[tuple[int, int]] = []

    class _DummyServer:
        num_steps = 4

        def _dispatch_client_timestep_change(
            self, client: object, timestep: int
        ) -> None:
            seen.append((getattr(client, "client_id"), timestep))

    playback = ClientPlaybackHandle.__new__(ClientPlaybackHandle)
    playback._server = _DummyServer()  # type: ignore[assignment]
    playback._client = SimpleNamespace(client_id=11)  # type: ignore[assignment]
    playback._lock = threading.RLock()  # type: ignore[assignment]
    playback._current_timestep = 0  # type: ignore[assignment]
    playback._is_playing = False  # type: ignore[assignment]
    playback._loop = False  # type: ignore[assignment]
    playback._sync_playback_buttons = lambda: None  # type: ignore[assignment]

    playback._sync_from_client(2)

    assert playback.current_timestep == 2
    assert seen == [(11, 2)]


def test_timeline_controller_emits_each_crossed_playback_step() -> None:
    class _DummyServer:
        num_steps = 5

        def _sync_client_playback_state(self, **_: object) -> None:
            return

    controller = TimelineController(_DummyServer(), fps=30.0)  # type: ignore[arg-type]
    try:
        seen: list[int] = []
        controller.on_timestep_change(seen.append)

        controller._advance_playback_timestep(3, loop=False)

        assert seen == [1, 2, 3]
    finally:
        controller.stop()


def test_timeline_controller_emits_wrapped_playback_steps() -> None:
    class _DummyServer:
        num_steps = 5

        def _sync_client_playback_state(self, **_: object) -> None:
            return

    controller = TimelineController(_DummyServer(), fps=30.0)  # type: ignore[arg-type]
    try:
        seen: list[int] = []
        controller.on_timestep_change(seen.append)

        controller.set_current_timestep(3)
        seen.clear()
        controller._advance_playback_timestep(1, loop=True)

        assert seen == [4, 0, 1]
    finally:
        controller.stop()


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
