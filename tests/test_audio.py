from collections.abc import Iterator

import numpy as np
import pytest
import viser4d


@pytest.fixture
def server() -> Iterator[viser4d.Viser4dServer]:
    server = viser4d.Viser4dServer(
        num_steps=10,
        host="127.0.0.1",
        port=0,
        verbose=False,
        enable_playback_gui=False,
    )
    try:
        yield server
    finally:
        server.stop()


def test_add_audio_smoke(server: viser4d.Viser4dServer) -> None:
    """Adding audio inside at(t) with different dtypes does not crash."""
    for dtype in (np.int16, np.float32):
        samples = np.zeros(100, dtype=dtype)
        with server.at(0):
            handle = server.scene.add_audio("/tone", data=samples, sample_rate=16000)
        handle.volume = 0.5
        handle.remove()


def test_add_audio_outside_context_raises(server: viser4d.Viser4dServer) -> None:
    """Calling add_audio outside at(t) raises RuntimeError."""
    samples = np.zeros(100, dtype=np.int16)
    with pytest.raises(RuntimeError, match="must be called inside"):
        server.scene.add_audio("/test", data=samples, sample_rate=16000)


def test_add_audio_same_name_rejects_sample_rate_mismatch(
    server: viser4d.Viser4dServer,
) -> None:
    with server.at(0):
        server.scene.add_audio(
            "/tone",
            data=np.zeros(32, dtype=np.float32),
            sample_rate=16000,
        )
    with server.at(1), pytest.raises(ValueError, match="sample_rate"):
        server.scene.add_audio(
            "/tone",
            data=np.zeros(32, dtype=np.float32),
            sample_rate=8000,
        )


def test_add_audio_same_name_rejects_channel_mismatch(
    server: viser4d.Viser4dServer,
) -> None:
    with server.at(0):
        server.scene.add_audio(
            "/tone",
            data=np.zeros(32, dtype=np.float32),
            sample_rate=16000,
        )
    with server.at(1), pytest.raises(ValueError):
        server.scene.add_audio(
            "/tone",
            data=np.zeros((32, 2), dtype=np.float32),
            sample_rate=16000,
        )


def test_audio_handle_append_extends_waveform(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle = server.scene.add_audio(
            "/tone",
            data=np.zeros(32, dtype=np.float32),
            sample_rate=16000,
        )

    handle.append(np.ones(16, dtype=np.float32))
    assert handle.waveform.shape == (48, 1)


def test_audio_handle_append_rejects_channel_mismatch(
    server: viser4d.Viser4dServer,
) -> None:
    with server.at(0):
        handle = server.scene.add_audio(
            "/tone",
            data=np.zeros((32, 2), dtype=np.float32),
            sample_rate=16000,
        )

    with pytest.raises(ValueError, match="channels"):
        handle.append(np.ones(16, dtype=np.float32))
