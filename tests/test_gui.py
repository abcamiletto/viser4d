import time
from collections.abc import Iterator
from threading import Event
from types import SimpleNamespace

import pytest
import viser4d


@pytest.fixture
def server() -> Iterator[viser4d.Viser4dServer]:
    server = viser4d.Viser4dServer(
        num_steps=10,
        host="127.0.0.1",
        port=0,
        verbose=False,
        enable_playback_gui=True,
    )
    try:
        yield server
    finally:
        server.stop()


def test_play_button_label_updates_before_slow_play(
    server: viser4d.Viser4dServer,
) -> None:
    controls = server._playback_controls
    assert controls is not None
    done = Event()

    def slow_play(fps: float, loop: bool = True) -> None:
        time.sleep(0.2)
        done.set()

    server.play = slow_play  # type: ignore[method-assign]

    started = time.perf_counter()
    controls._on_play_button(None)
    elapsed = time.perf_counter() - started

    assert controls._play_button.label == "Pause"
    assert elapsed < 0.1
    assert done.wait(timeout=1.0)


def test_pause_button_label_updates_before_slow_pause(
    server: viser4d.Viser4dServer,
) -> None:
    controls = server._playback_controls
    assert controls is not None
    done = Event()

    controls._playing = True
    controls._play_button.label = "Pause"

    def slow_pause() -> None:
        time.sleep(0.2)
        done.set()

    server.pause = slow_pause  # type: ignore[method-assign]

    started = time.perf_counter()
    controls._on_play_button(None)
    elapsed = time.perf_counter() - started

    assert controls._play_button.label == "Play"
    assert elapsed < 0.1
    assert done.wait(timeout=1.0)


def test_slider_seek_returns_before_slow_seek(server: viser4d.Viser4dServer) -> None:
    controls = server._playback_controls
    assert controls is not None
    done = Event()

    def slow_seek(t: int) -> None:
        time.sleep(0.2)
        done.set()

    server.seek = slow_seek  # type: ignore[method-assign]

    event = SimpleNamespace(
        client_id=1,
        target=SimpleNamespace(value=3),
    )
    started = time.perf_counter()
    controls._on_slider(event)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1
    assert done.wait(timeout=1.0)


def test_step_button_returns_before_slow_seek(server: viser4d.Viser4dServer) -> None:
    controls = server._playback_controls
    assert controls is not None
    done = Event()

    def slow_seek(t: int) -> None:
        time.sleep(0.2)
        done.set()

    server.seek = slow_seek  # type: ignore[method-assign]
    controls._slider.value = 4

    event = SimpleNamespace(target=SimpleNamespace(value="Next"))
    started = time.perf_counter()
    controls._on_step(event)
    elapsed = time.perf_counter() - started

    assert elapsed < 0.1
    assert done.wait(timeout=1.0)
