from collections.abc import Callable, Iterator
import threading
import time
from pathlib import Path

import msgspec
import pytest
import viser4d
import zstandard


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


def _decode_viser_bytes(data: bytes) -> dict[str, object]:
    uncompressed_size = int.from_bytes(data[:8], "little")
    payload = zstandard.ZstdDecompressor().decompress(
        data[8:], max_output_size=uncompressed_size
    )
    decoded = msgspec.msgpack.decode(payload)
    assert isinstance(decoded, dict)
    return decoded


def test_seek_applies_recorded_updates(server: viser4d.Viser4dServer) -> None:
    handle = None
    for t in range(3):
        with server.at(t):
            if handle is None:
                handle = server.scene.add_frame("/frame", axes_length=0.1)
            handle.position = (float(t), 0.0, 0.0)

    assert handle is not None

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 0 and _d.set())

    server.seek(0)

    assert done.wait(timeout=1.0)
    assert _position(handle) == (0.0, 0.0, 0.0)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 2 and _d.set())

    server.seek(2)

    assert done.wait(timeout=1.0)
    assert _position(handle) == (2.0, 0.0, 0.0)


def test_seek_backwards_removes_late_adds(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle_a = server.scene.add_frame("/a", axes_length=0.1)
    with server.at(1):
        handle_b = server.scene.add_frame("/b", axes_length=0.1)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 1 and _d.set())

    server.seek(1)

    assert done.wait(timeout=1.0)
    _ = _position(handle_a)
    _ = _position(handle_b)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 0 and _d.set())

    server.seek(0)

    assert done.wait(timeout=1.0)
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

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 1 and _d.set())

    server.seek(1)

    assert done.wait(timeout=1.0)
    assert tuple(handle.position) == (0.0, 0.0, 0.0)
    assert tuple(handle.wxyz) == (1.0, 0.0, 0.0, 0.0)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 2 and _d.set())

    server.seek(2)

    assert done.wait(timeout=1.0)
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

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 1 and _d.set())

    server.seek(1)

    assert done.wait(timeout=1.0)
    assert _position(a) == (0.0, 0.0, 0.0)
    assert _position(b) == (1.0, 0.0, 0.0)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 2 and _d.set())

    server.seek(2)

    assert done.wait(timeout=1.0)
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

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 0 and _d.set())

    server.seek(0)

    assert done.wait(timeout=1.0)
    assert _position(handle) == (0.0, 0.0, 0.0)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 1 and _d.set())

    server.seek(1)

    assert done.wait(timeout=1.0)
    _assert_missing_handle(handle)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 2 and _d.set())

    server.seek(2)

    assert done.wait(timeout=1.0)
    assert _position(handle) == (2.0, 0.0, 0.0)


def test_handle_remove_is_recorded(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/a", axes_length=0.1)
    with server.at(1):
        handle.remove()
    with server.at(2):
        same_name_handle = server.scene.add_frame("/a", axes_length=0.1)
        same_name_handle.position = (2.0, 0.0, 0.0)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 0 and _d.set())

    server.seek(0)

    assert done.wait(timeout=1.0)
    assert _position(handle) == (0.0, 0.0, 0.0)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 1 and _d.set())

    server.seek(1)

    assert done.wait(timeout=1.0)
    _assert_missing_handle(handle)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 2 and _d.set())

    server.seek(2)

    assert done.wait(timeout=1.0)
    assert _position(handle) == (2.0, 0.0, 0.0)


def test_play_loops_by_default(server: viser4d.Viser4dServer) -> None:
    seen_steps: list[int] = []
    server.on_timestep_change(lambda t: seen_steps.append(t))

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 2 and _d.set())

    server.seek(2)

    assert done.wait(timeout=1.0)
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

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 0 and _d.set())

    server.seek(0)

    assert done.wait(timeout=1.0)
    done = threading.Event()
    server.on_timestep_change(lambda step, _d=done: step == 2 and _d.set())
    server.seek(2)
    assert done.wait(timeout=1.0)
    done = threading.Event()
    server.on_timestep_change(lambda step, _d=done: step == 1 and _d.set())
    server.seek(1)
    assert done.wait(timeout=1.0)
    assert called_with == [0, 2, 1]


def test_timestep_callbacks_run_on_worker_thread(
    server: viser4d.Viser4dServer,
) -> None:
    loop_thread_ids: list[int] = []
    loop_ready = threading.Event()

    def _capture_loop_thread_id() -> None:
        loop_thread_ids.append(threading.get_ident())
        loop_ready.set()

    server.get_event_loop().call_soon_threadsafe(_capture_loop_thread_id)
    assert loop_ready.wait(timeout=1.0)

    callback_thread_ids: list[int] = []
    callback_done = threading.Event()

    def _on_step(_t: int) -> None:
        callback_thread_ids.append(threading.get_ident())
        callback_done.set()

    server.on_timestep_change(_on_step)
    server.seek(0)

    assert callback_done.wait(timeout=1.0)
    assert loop_thread_ids
    assert callback_thread_ids
    assert callback_thread_ids[0] != loop_thread_ids[0]


def test_multiple_timestep_callbacks(server: viser4d.Viser4dServer) -> None:
    results: list[str] = []

    server.on_timestep_change(lambda t: results.append(f"a:{t}"))
    server.on_timestep_change(lambda t: results.append(f"b:{t}"))

    server.seek(1)

    assert _wait_until(lambda: sorted(results) == ["a:1", "b:1"])


def test_current_time_property(server: viser4d.Viser4dServer) -> None:
    assert server.current_time == 0

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 2 and _d.set())

    server.seek(2)

    assert done.wait(timeout=1.0)
    assert server.current_time == 2

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 1 and _d.set())

    server.seek(1)

    assert done.wait(timeout=1.0)
    assert server.current_time == 1


def test_proxy_handle_can_read_and_write_live_state(
    server: viser4d.Viser4dServer,
) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (1.0, 2.0, 3.0)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 0 and _d.set())

    server.seek(0)

    assert done.wait(timeout=1.0)
    assert tuple(handle.position) == (1.0, 2.0, 3.0)

    handle.position = (5.0, 5.0, 5.0)
    assert _position(handle) == (5.0, 5.0, 5.0)


def test_proxy_handle_live_write_persists_across_seek(
    server: viser4d.Viser4dServer,
) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (0.0, 0.0, 0.0)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 0 and _d.set())

    server.seek(0)

    assert done.wait(timeout=1.0)
    handle.visible = False

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 0 and _d.set())

    server.seek(0)

    assert done.wait(timeout=1.0)
    assert handle.visible is False


def test_recorded_change_overwrites_live_change(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (0.0, 0.0, 0.0)
    with server.at(1):
        handle.position = (1.0, 1.0, 1.0)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 0 and _d.set())

    server.seek(0)

    assert done.wait(timeout=1.0)
    handle.position = (5.0, 5.0, 5.0)

    done = threading.Event()

    server.on_timestep_change(lambda step, _d=done: step == 1 and _d.set())

    server.seek(1)

    assert done.wait(timeout=1.0)
    assert _position(handle) == (1.0, 1.0, 1.0)


def test_proxy_handle_error_before_seek(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)

    with pytest.raises(RuntimeError, match="not in live scene"):
        _ = handle.position


def test_serialize_integration_with_real_state_serializer(
    server: viser4d.Viser4dServer, tmp_path: Path
) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (0.0, 0.0, 0.0)
    with server.at(1):
        handle.position = (1.0, 0.0, 0.0)
    with server.at(2):
        handle.position = (2.0, 0.0, 0.0)

    output = tmp_path / "recording.viser"
    data = server.serialize(output)

    assert output.exists()
    assert output.read_bytes() == data
    assert len(data) > 8
    assert int.from_bytes(data[:8], "little") > 0
    decoded = _decode_viser_bytes(data)
    assert decoded["durationSeconds"] == pytest.approx(3.0 / 30.0)
    messages = decoded["messages"]
    assert isinstance(messages, list)
    assert len(messages) > 0
