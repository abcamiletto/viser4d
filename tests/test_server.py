from __future__ import annotations

import base64
import pathlib

import msgspec
import numpy as np
import pytest
import zstandard

import viser4d
from viser4d._audio import audio_array_payload


def test_audio_requires_timestep_context() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with pytest.raises(RuntimeError):
            server.scene.add_audio("/audio", data=np.zeros(4, dtype=np.int16), sample_rate=8_000)
    finally:
        server.stop()


def test_num_steps_must_be_positive() -> None:
    with pytest.raises(ValueError, match="num_steps must be >= 1"):
        viser4d.Viser4dServer(num_steps=0, port=0, verbose=False)


def test_timeline_records_scene_and_audio(tmp_path: pathlib.Path) -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        seen_timesteps: list[int] = []
        server.on_timestep_change(seen_timesteps.append)

        with server.at(0):
            frame = server.scene.add_frame("/frame")
            audio = server.scene.add_audio(
                "/audio",
                data=np.array([0, 1, 2], dtype=np.int16),
                sample_rate=16_000,
            )
        with server.at(1):
            frame.position = (1.0, 2.0, 3.0)
            audio.volume = 0.25

        assert server._timeline.step(0).messages
        assert server._timeline.step(0).audio_ops
        assert server._timeline.step(1).messages
        assert server._timeline.step(1).audio_ops

        server._fps_slider.value = 24.0
        assert server._fps == 24.0

        server.play(server._fps)
        assert server._play_button.visible is False
        assert server._pause_button.visible is True
        server.pause()
        assert server._play_button.visible is True
        assert server._pause_button.visible is False

        server.seek(2)
        assert server._timeline_slider.value == 2
        assert seen_timesteps[-1] == 2

        out_path = tmp_path / "scene.viser4d"
        blob = server.serialize(out_path)
        assert blob
        assert out_path.read_bytes() == blob

        size = int.from_bytes(blob[:8], "little")
        decoded = zstandard.ZstdDecompressor().decompress(blob[8:], size)
        payload = msgspec.msgpack.decode(decoded)
        assert set(payload) == {"durationSeconds", "messages", "viserVersion"}
        assert any(
            message["type"] == "RunJavascriptMessage"
            for _, message in payload["messages"]
        )
    finally:
        server.stop()


def test_serialize_rejects_invalid_timestep_range(tmp_path: pathlib.Path) -> None:
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with pytest.raises(ValueError, match="Invalid timestep range"):
            server.serialize(tmp_path / "scene.viser4d", start_timestep=2, end_timestep=1)
    finally:
        server.stop()


def test_stop_shuts_down_predictor_thread() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    predictor_thread = server._controller._predictor_thread
    assert predictor_thread.is_alive()

    server.stop()

    assert predictor_thread.is_alive() is False


def test_audio_payload_normalizes_integer_formats() -> None:
    int16_payload = audio_array_payload(np.array([-32768, 0, 32767], dtype=np.int16))
    int16_values = np.frombuffer(
        base64.b64decode(int16_payload["data"]), dtype=np.float32
    )
    assert int16_payload["dtype"] == "float32"
    assert np.allclose(int16_values, np.array([-1.0, 0.0, 32767 / 32768], dtype=np.float32))

    int32_payload = audio_array_payload(
        np.array([-2147483648, 0, 2147483647], dtype=np.int32)
    )
    int32_values = np.frombuffer(
        base64.b64decode(int32_payload["data"]), dtype=np.float32
    )
    assert int32_payload["dtype"] == "float32"
    assert np.allclose(
        int32_values,
        np.array([-1.0, 0.0, 2147483647 / 2147483648], dtype=np.float32),
    )

def test_audio_append_keeps_chunked_state_until_waveform_is_read() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0):
            audio = server.scene.add_audio(
                "/audio",
                data=np.array([1, 2], dtype=np.int16),
                sample_rate=16_000,
            )

        audio.append(np.array([3, 4], dtype=np.int16))
        audio.append(np.array([5, 6], dtype=np.int16))

        expected = np.array([1, 2, 3, 4, 5, 6], dtype=np.int16)
        assert np.array_equal(audio.waveform, expected)
        assert np.array_equal(audio._state.waveform, expected)
    finally:
        server.stop()
