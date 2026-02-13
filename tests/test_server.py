from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
import viser4d


@pytest.fixture
def server() -> Iterator[viser4d.Viser4dServer]:
    server = viser4d.Viser4dServer(
        num_steps=3,
        host="127.0.0.1",
        port=0,
        verbose=False,
        enable_playback_gui=False,
    )
    try:
        yield server
    finally:
        server.stop()


def _position(handle: object) -> tuple[float, float, float]:
    return tuple(handle.position)  # type: ignore[attr-defined]


def _assert_missing_handle(handle: object) -> None:
    with pytest.raises(RuntimeError, match="not in live scene"):
        _ = handle.position  # type: ignore[attr-defined]


def _wait_until(
    predicate: Callable[[], bool], timeout: float = 1.0, interval: float = 0.01
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_seek_applies_recorded_updates(server: viser4d.Viser4dServer) -> None:
    handle = None
    for t in range(3):
        with server.at(t):
            if handle is None:
                handle = server.scene.add_frame("/frame", axes_length=0.1)
            handle.position = (float(t), 0.0, 0.0)

    assert handle is not None

    server.seek(0)
    assert _position(handle) == (0.0, 0.0, 0.0)

    server.seek(2)
    assert _position(handle) == (2.0, 0.0, 0.0)


def test_seek_backwards_removes_late_adds(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle_a = server.scene.add_frame("/a", axes_length=0.1)
    with server.at(1):
        handle_b = server.scene.add_frame("/b", axes_length=0.1)

    server.seek(1)
    _ = _position(handle_a)
    _ = _position(handle_b)

    server.seek(0)
    _ = _position(handle_a)
    _assert_missing_handle(handle_b)


def test_seek_uses_latest_attribute_value(server: viser4d.Viser4dServer) -> None:
    handle = None
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (0.0, 0.0, 0.0)
    with server.at(1):
        assert handle is not None
        handle.wxyz = (1.0, 0.0, 0.0, 0.0)
    with server.at(2):
        assert handle is not None
        handle.position = (2.0, 0.0, 0.0)

    assert handle is not None

    server.seek(1)
    assert tuple(handle.position) == (0.0, 0.0, 0.0)
    assert tuple(handle.wxyz) == (1.0, 0.0, 0.0, 0.0)

    server.seek(2)
    assert tuple(handle.position) == (2.0, 0.0, 0.0)
    assert tuple(handle.wxyz) == (1.0, 0.0, 0.0, 0.0)


def test_seek_multiple_objects(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        a = server.scene.add_frame("/a", axes_length=0.1)
        a.position = (0.0, 0.0, 0.0)
    with server.at(1):
        b = server.scene.add_frame("/b", axes_length=0.1)
        b.position = (1.0, 0.0, 0.0)
    with server.at(2):
        a.position = (2.0, 0.0, 0.0)
        b.position = (2.0, 0.0, 0.0)

    server.seek(1)
    assert _position(a) == (0.0, 0.0, 0.0)
    assert _position(b) == (1.0, 0.0, 0.0)

    server.seek(2)
    assert _position(a) == (2.0, 0.0, 0.0)
    assert _position(b) == (2.0, 0.0, 0.0)


def test_remove_by_name_is_recorded(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/a", axes_length=0.1)
    with server.at(1):
        server.scene.remove_by_name("/a")
    with server.at(2):
        same_name_handle = server.scene.add_frame("/a", axes_length=0.1)
        same_name_handle.position = (2.0, 0.0, 0.0)

    server.seek(0)
    assert _position(handle) == (0.0, 0.0, 0.0)

    server.seek(1)
    _assert_missing_handle(handle)

    server.seek(2)
    assert _position(handle) == (2.0, 0.0, 0.0)


def test_handle_remove_is_recorded(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/a", axes_length=0.1)
    with server.at(1):
        handle.remove()
    with server.at(2):
        same_name_handle = server.scene.add_frame("/a", axes_length=0.1)
        same_name_handle.position = (2.0, 0.0, 0.0)

    server.seek(0)
    assert _position(handle) == (0.0, 0.0, 0.0)

    server.seek(1)
    _assert_missing_handle(handle)

    server.seek(2)
    assert _position(handle) == (2.0, 0.0, 0.0)


def test_play_loops_by_default(server: viser4d.Viser4dServer) -> None:
    seen_steps: list[int] = []
    server.on_timestep_change(lambda t: seen_steps.append(t))

    server.seek(2)
    seen_steps.clear()
    server.play(fps=120)

    try:
        assert _wait_until(lambda: 0 in seen_steps)
    finally:
        server.pause()


def test_on_timestep_change_callback(server: viser4d.Viser4dServer) -> None:
    called_with: list[int] = []

    def callback(t: int) -> None:
        called_with.append(t)

    server.on_timestep_change(callback)

    server.seek(0)
    server.seek(2)
    server.seek(1)

    assert called_with == [0, 2, 1]


def test_multiple_timestep_callbacks(server: viser4d.Viser4dServer) -> None:
    results: list[str] = []

    server.on_timestep_change(lambda t: results.append(f"a:{t}"))
    server.on_timestep_change(lambda t: results.append(f"b:{t}"))

    server.seek(1)

    assert results == ["a:1", "b:1"]


def test_current_time_property(server: viser4d.Viser4dServer) -> None:
    assert server.current_time == 0

    server.seek(2)
    assert server.current_time == 2

    server.seek(1)
    assert server.current_time == 1


def test_proxy_handle_can_read_and_write_live_state(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (1.0, 2.0, 3.0)

    server.seek(0)
    assert tuple(handle.position) == (1.0, 2.0, 3.0)

    handle.position = (5.0, 5.0, 5.0)
    assert _position(handle) == (5.0, 5.0, 5.0)


def test_proxy_handle_live_write_persists_across_seek(
    server: viser4d.Viser4dServer,
) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (0.0, 0.0, 0.0)

    server.seek(0)
    handle.visible = False

    server.seek(0)
    assert handle.visible is False


def test_recorded_change_overwrites_live_change(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (0.0, 0.0, 0.0)
    with server.at(1):
        handle.position = (1.0, 1.0, 1.0)

    server.seek(0)
    handle.position = (5.0, 5.0, 5.0)

    server.seek(1)
    assert _position(handle) == (1.0, 1.0, 1.0)


def test_proxy_handle_error_before_seek(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)

    with pytest.raises(RuntimeError, match="not in live scene"):
        _ = handle.position


def test_at_context_is_thread_local(server: viser4d.Viser4dServer) -> None:
    barrier = threading.Barrier(2)

    def record(name: str, t: int, x: float):
        with server.at(t):
            handle = server.scene.add_frame(name, axes_length=0.1)
            barrier.wait(timeout=2.0)
            handle.position = (x, 0.0, 0.0)
            return handle

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(record, "/a", 0, 0.0)
        future_b = pool.submit(record, "/b", 2, 2.0)
        handle_a = future_a.result(timeout=2.0)
        handle_b = future_b.result(timeout=2.0)

    server.seek(0)
    assert _position(handle_a) == (0.0, 0.0, 0.0)
    _assert_missing_handle(handle_b)

    server.seek(2)
    assert _position(handle_a) == (0.0, 0.0, 0.0)
    assert _position(handle_b) == (2.0, 0.0, 0.0)
