import threading
import time

import msgspec
import numpy as np
import pytest
import zstandard

import viser4d


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
        server.play(24.0)
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


def test_refresh_redraws_current_timestep_without_seeking() -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)

    class _PlaybackStub:
        def __init__(self) -> None:
            self.refresh_calls = 0

        def refresh(self) -> None:
            self.refresh_calls += 1

    try:
        server.seek(1)
        playback = _PlaybackStub()
        server._client_playback_values = lambda: [playback]  # type: ignore[method-assign]
        seen_timesteps: list[int] = []
        server.on_timestep_change(seen_timesteps.append)

        server.refresh()

        assert server._current_timestep == 1
        assert playback.refresh_calls == 1
        assert seen_timesteps == []
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


def test_serialize_preserves_binary_mesh_payloads() -> None:
    server = viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)
    try:
        vertices = np.array(
            [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
            dtype=np.float32,
        )
        faces = np.array([[0, 1, 2]], dtype=np.uint32)
        with server.at(0):
            server.scene.add_mesh_simple(
                "/mesh",
                vertices=vertices,
                faces=faces,
            )

        recording = server.serialize()
        size = int.from_bytes(recording[:8], "little")
        packed = zstandard.ZstdDecompressor().decompress(recording[8:], size)
        decoded = msgspec.msgpack.decode(packed)
        mesh_message = next(
            message
            for _, message in decoded["messages"]
            if message.get("type") == "MeshMessage"
        )

        assert isinstance(mesh_message["props"]["vertices"], bytes)
        assert isinstance(mesh_message["props"]["faces"], bytes)
    finally:
        server.stop()
