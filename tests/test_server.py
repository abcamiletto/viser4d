import threading
import time
from typing import cast

import msgspec
import numpy as np
import pytest
import zstandard

import viser4d


def _deserialize_recording(blob: bytes) -> dict[str, object]:
    hybrid_size = int.from_bytes(blob[:8], "little")
    hybrid = zstandard.ZstdDecompressor().decompress(
        blob[8:], max_output_size=hybrid_size
    )
    msgpack_size = int.from_bytes(hybrid[:8], "little")
    return cast(dict[str, object], msgspec.msgpack.decode(hybrid[8 : 8 + msgpack_size]))


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
