from typing import Any, cast

import numpy as np
import pytest
from helpers import deserialize_recording

import viser4d


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


def test_audio_rejects_non_positive_sample_rate() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with (
            server.at(0) as tl,
            pytest.raises(ValueError, match="sample_rate must be a positive integer"),
        ):
            tl.audio.add_track(
                "/audio", data=np.array([1, 2], dtype=np.int16), sample_rate=0
            )
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
        with pytest.raises(RuntimeError, match="only valid inside server.at\\(t\\)"):
            audio.append(np.array([3, 4], dtype=np.int16))
        with pytest.raises(RuntimeError, match="only valid inside server.at\\(t\\)"):
            audio.remove()
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
        recording = deserialize_recording(server.serialize())
        messages = cast(list[tuple[float, dict[str, object]]], recording["messages"])
        appends = [
            m
            for _, m in messages
            if m.get("type") == "AppendAudioMessage" and m.get("name") == "/audio"
        ]
        assert len(appends) == 2
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

        recording = deserialize_recording(server.serialize(start_timestep=1))
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
