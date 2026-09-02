import threading
import time
from concurrent.futures import Future
from types import SimpleNamespace
from typing import Any, cast

import pytest

import viser4d
from viser4d import _server as server_module
from viser4d._config import PlaybackConfig
from viser4d._playback import ClientSession
from viser4d._protocol import (
    TimelineBlockBytesMessage,
    TimelineBlockMessage,
    TimelineOverrideMessage,
    TimelinePlayMessage,
    TimelineReadyMessage,
    TimelineSetSpeedMessage,
)
from viser4d._state import SceneEntryRecord, StoredMessage, scene_puts_deletes


def test_play_takes_no_arguments() -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)
    untyped = cast(Any, server)
    try:
        with pytest.raises(TypeError, match="unexpected keyword argument 'loop'"):
            untyped.play(loop=True)
        with pytest.raises(TypeError, match="unexpected keyword argument 'speed'"):
            untyped.play(speed=2.0)
    finally:
        server.stop()


def test_server_playback_config_propagates_to_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = viser4d.Viser4dServer(
        num_steps=2, loop=True, playback_speed=1.5, port=0, verbose=False
    )

    class FakeSession:
        def __init__(
            self, client: Any, _timeline: Any, config: PlaybackConfig, **_kwargs: Any
        ) -> None:
            self.client_id = client.client_id
            self.loop_on_init = config.loop
            self.speed_on_init = config.speed
            self.config_syncs = 0
            self.play_calls = 0

        def start(self) -> None:
            pass

        def play(self) -> None:
            self.play_calls += 1

        def sync_config(self) -> None:
            self.config_syncs += 1

        def set_speed(self, speed: float) -> None:
            self.speed_on_init = speed

        def handle_event(self, _message: object) -> None:
            pass

    monkeypatch.setattr(server_module, "ClientSession", FakeSession)
    try:
        attach = server._client_connect_cb[-1]
        attach(cast(Any, SimpleNamespace(client_id=123)))
        first = server.get_client_playback(123)
        assert isinstance(first, FakeSession)
        assert first.loop_on_init is True
        assert first.speed_on_init == 1.5

        server.set_loop(False)
        server.set_playback_speed(0.5)
        assert first.config_syncs == 1
        assert first.speed_on_init == 0.5
        assert server.loop is False
        assert server.playback_speed == 0.5

        attach(cast(Any, SimpleNamespace(client_id=456)))
        second = server.get_client_playback(456)
        assert isinstance(second, FakeSession)
        assert second.loop_on_init is False
        assert second.speed_on_init == 0.5

        server.play()
        assert first.play_calls == 1
        assert second.play_calls == 1
    finally:
        server.stop()


def _playback_config(**overrides: Any) -> PlaybackConfig:
    base: dict[str, Any] = {
        "fps": 1.0,
        "streaming": viser4d.StreamingConfig(block_size=32, client_cache_bytes=1000),
        "loop": True,
        "speed": 2.0,
    }
    base.update(overrides)
    return PlaybackConfig(**base)


def _fake_timeline(**overrides: Any) -> Any:
    base: dict[str, Any] = {
        "num_steps": 2,
        "block_bytes": list,
        "override_items": list,
    }
    base.update(overrides)
    return cast(Any, SimpleNamespace(**base))


def _ready_session(
    timeline: Any = None, config: PlaybackConfig | None = None
) -> tuple[ClientSession, list[Any]]:
    """A session wired to fakes, already past TimelineReadyMessage."""
    messages: list[Any] = []
    client = cast(
        Any,
        SimpleNamespace(
            client_id=1,
            _websock_connection=SimpleNamespace(queue_message=messages.append),
        ),
    )
    session = ClientSession(
        client,
        timeline if timeline is not None else _fake_timeline(),
        config if config is not None else _playback_config(),
        executor=cast(Any, None),
        event_loop=cast(Any, None),
        on_timestep=lambda _client, _step: None,
        on_playback=lambda _client, _playing: None,
    )
    session.handle_event(TimelineReadyMessage())
    return session, messages


def test_client_session_uses_current_playback_config() -> None:
    config = _playback_config()
    session, messages = _ready_session(config=config)

    messages.clear()
    session.play()
    assert isinstance(messages[-1], TimelinePlayMessage)
    assert messages[-1].speed == 2.0
    assert messages[-1].loop is True

    config.loop = False
    session.set_speed(0.5)
    assert isinstance(messages[-1], TimelineSetSpeedMessage)
    assert messages[-1].speed == 0.5
    assert messages[-1].loop is False


def test_client_session_syncs_existing_overrides_on_start() -> None:
    stored = StoredMessage(
        {"type": "SetPositionMessage", "name": "/frame", "position": [1.0, 2.0, 3.0]}
    )
    puts, _ = scene_puts_deletes(stored)
    key, name, message = puts[0]
    entry = SceneEntryRecord(key, 1, name, message)

    session, messages = _ready_session(_fake_timeline(override_items=lambda: [entry]))
    session.start()

    overrides = [m for m in messages if isinstance(m, TimelineOverrideMessage)]
    assert len(overrides) == 1
    assert overrides[0].entry["key"] == key
    assert overrides[0].entry["message"]["type"] == "SetPositionMessage"


def test_block_request_sends_fresh_block_bytes() -> None:
    block_bytes = [123]
    session, messages = _ready_session(_fake_timeline(block_bytes=lambda: block_bytes))
    session._pending_requests.add(0)
    future: Future[Any] = Future()
    future.set_result(
        TimelineBlockMessage(index=0, checkpointScene=[], checkpointAudio=[], deltas=[])
    )

    session._finish_block_request(0, future)

    assert isinstance(messages[-2], TimelineBlockMessage)
    assert isinstance(messages[-1], TimelineBlockBytesMessage)
    assert messages[-1].blockBytes == block_bytes
    assert session.loaded_blocks == {0}


def test_block_request_failure_propagates() -> None:
    session, _messages = _ready_session()
    session._pending_requests.add(0)
    future: Future[Any] = Future()
    future.set_exception(RuntimeError("block build failed"))

    with pytest.raises(RuntimeError, match="block build failed"):
        session._finish_block_request(0, future)


def test_runtime_ready_before_attach_is_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = viser4d.Viser4dServer(num_steps=2, port=0, verbose=False)

    class FakeSession:
        def __init__(self, *_a: Any, **_k: Any) -> None:
            self.events: list[str] = []

        def start(self) -> None:
            pass

        def handle_event(self, message: object) -> None:
            self.events.append(type(message).__name__)

    monkeypatch.setattr(server_module, "ClientSession", FakeSession)
    try:
        server._handle_event(123, TimelineReadyMessage())
        attach = server._client_connect_cb[-1]
        attach(cast(Any, SimpleNamespace(client_id=123)))
        session = server.get_client_playback(123)
        assert isinstance(session, FakeSession)
        assert session.events == ["TimelineReadyMessage"]
        assert 123 not in server._pending_ready_ids
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
