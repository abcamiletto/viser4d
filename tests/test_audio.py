import numpy as np
import pytest
from helpers import exported_timeline

import viser4d


def test_audio_samples_reflects_appended_data() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            audio = tl.audio.add(
                "/audio", samples=np.array([1, 2], dtype=np.int16), sample_rate=16_000
            )
        with server.at(1):
            audio.append(np.array([3, 4], dtype=np.int16))
            audio.append(np.array([5, 6], dtype=np.int16))
        assert np.array_equal(
            audio.samples, np.array([1, 2, 3, 4, 5, 6], dtype=np.float32) / 32768
        )
    finally:
        server.stop()


def test_audio_rejects_invalid_shapes() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            with pytest.raises(ValueError, match="shape"):
                tl.audio.add(
                    "/audio",
                    samples=np.zeros((2, 2, 2), dtype=np.float32),
                    sample_rate=16_000,
                )
            with pytest.raises(ValueError, match="shape"):
                tl.audio.add(
                    "/audio-33ch",
                    samples=np.zeros((4, 33), dtype=np.float32),
                    sample_rate=16_000,
                )
    finally:
        server.stop()


def test_stereo_audio_append_preserves_layout() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            audio = tl.audio.add(
                "/audio",
                samples=np.array([[1, 10], [2, 20]], dtype=np.int16),
                sample_rate=16_000,
            )
        with server.at(1):
            audio.append(np.array([[3, 30], [4, 40]], dtype=np.int16))
            expected = np.array([[1, 10], [2, 20], [3, 30], [4, 40]], dtype=np.int16)
            assert np.array_equal(audio.samples, expected.astype(np.float32) / 32768)
            with pytest.raises(ValueError, match="channel"):
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
            tl.audio.add(
                "/audio", samples=np.array([1, 2], dtype=np.int16), sample_rate=0
            )
    finally:
        server.stop()


def test_out_of_session_audio_edits_raise() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            audio = tl.audio.add(
                "/audio", samples=np.array([1, 2], dtype=np.int16), sample_rate=16_000
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
            audio = tl.audio.add(
                "/audio", samples=np.array([1, 2], dtype=np.int16), sample_rate=16_000
            )
        with server.at(1):
            audio.append(np.array([3, 4], dtype=np.int16))
            audio.append(np.array([5, 6], dtype=np.int16))
        recording = exported_timeline(server.serialize())
        events = recording["block"]["deltas"][1]["audio"]
        appends = [m for m in events if m["type"] == "AudioAppendMessage"]
        assert len(appends) == 2
    finally:
        server.stop()


def test_export_preserves_preroll_audio_anchors() -> None:
    server = viser4d.Viser4dServer(num_steps=4, fps=2.0, port=0, verbose=False)
    try:
        with server.at(0) as tl:
            tl.audio.add(
                "/audio", samples=np.arange(8, dtype=np.float32), sample_rate=8
            )
            tl.audio.add("/short", samples=np.zeros(2, dtype=np.float32), sample_rate=8)

        recording = exported_timeline(server.serialize(start_timestep=1))
        tracks = {m["name"]: m for m in recording["block"]["checkpointAudio"]}
        # Complete samples and a negative anchor preserve future edits to either
        # track; the shared engine decides whether it has already finished.
        assert set(tracks) == {"/audio", "/short"}
        assert tracks["/audio"]["start_time"] == -0.5
        assert tracks["/audio"]["num_channels"] == 1
        assert tracks["/audio"]["samples"]["__typed_array"] == "<f4"
    finally:
        server.stop()


def test_audio_samples_survives_checkpoint_fold() -> None:
    server = viser4d.Viser4dServer(
        num_steps=3,
        streaming=viser4d.StreamingConfig(block_size=1),
        port=0,
        verbose=False,
    )
    try:
        with server.at(0) as tl:
            audio = tl.audio.add(
                "/audio", samples=np.array([1, 2], dtype=np.int16), sample_rate=8_000
            )
        with server.at(1):
            audio.append(np.array([3, 4], dtype=np.int16))
        message = server._timeline.block_message(2)
        assert len(message.checkpointAudio) == 1
        track = message.checkpointAudio[0]
        assert track["name"] == "/audio"
        assert track["start_time"] == 0
        assert track["sample_rate"] == 8_000
        assert len(track["samples"]) == 4
    finally:
        server.stop()
