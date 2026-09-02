import pytest

import viser4d


def test_num_steps_must_be_positive() -> None:
    with pytest.raises(ValueError, match="num_steps must be >= 1"):
        viser4d.Viser4dServer(num_steps=0, port=0, verbose=False)
    server = viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)
    try:
        with pytest.raises(ValueError, match="num_steps must be >= 1"):
            server.set_steps(0)
    finally:
        server.stop()


def test_fps_and_speed_must_be_positive() -> None:
    with pytest.raises(ValueError, match="fps must be a positive finite float"):
        viser4d.Viser4dServer(num_steps=1, fps=0.0, port=0, verbose=False)
    with pytest.raises(
        ValueError, match="playback_speed must be a positive finite float"
    ):
        viser4d.Viser4dServer(num_steps=1, playback_speed=0.0, port=0, verbose=False)
    server = viser4d.Viser4dServer(num_steps=3, port=0, verbose=False)
    try:
        with pytest.raises(ValueError, match="speed must be a positive finite float"):
            server.set_playback_speed(-1.0)
    finally:
        server.stop()


def test_client_cache_size_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISER4D_CLIENT_CHUNK_CACHE_SIZE", "2000000")
    server = viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)
    try:
        assert server.streaming.client_cache_bytes == 2_000_000
    finally:
        server.stop()


def test_block_size_comes_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISER4D_BLOCK_SIZE", "16")
    server = viser4d.Viser4dServer(num_steps=100, port=0, verbose=False)
    try:
        assert server.block_size == 16
    finally:
        server.stop()


def test_block_size_rejects_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISER4D_BLOCK_SIZE", "invalid")
    with pytest.raises(
        ValueError, match="VISER4D_BLOCK_SIZE must be a positive integer"
    ):
        viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)


def test_client_cache_size_rejects_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VISER4D_CLIENT_CHUNK_CACHE_SIZE", "invalid")
    with pytest.raises(
        ValueError,
        match="VISER4D_CLIENT_CHUNK_CACHE_SIZE must be a positive integer",
    ):
        viser4d.Viser4dServer(num_steps=1, port=0, verbose=False)


def test_streaming_config_is_public_and_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VISER4D_BLOCK_SIZE", "16")
    monkeypatch.setenv("VISER4D_CLIENT_CHUNK_CACHE_SIZE", "2000000")
    config = viser4d.StreamingConfig(block_size=8, client_cache_bytes=1234)
    server = viser4d.Viser4dServer(
        num_steps=100, streaming=config, port=0, verbose=False
    )
    try:
        assert server.streaming == config
        assert server.block_size == 8
        assert server.streaming.client_cache_bytes == 1234
    finally:
        server.stop()


def test_server_uses_32_step_blocks_by_default() -> None:
    server = viser4d.Viser4dServer(num_steps=100, port=0, verbose=False)
    try:
        assert server.block_size == 32
    finally:
        server.stop()
