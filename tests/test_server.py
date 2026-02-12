import asyncio
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
import threading

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


def test_seek_applies_recorded_updates(server: viser4d.Viser4dServer) -> None:
    handle = None
    for t in range(3):
        with server.at(t):
            if handle is None:
                handle = server.scene.add_frame("/frame", axes_length=0.1)
            handle.position = (float(t), 0.0, 0.0)

    server.seek(0)
    handle0 = server._live_scene._handle_from_node_name["/frame"]
    assert tuple(handle0.position) == (0.0, 0.0, 0.0)

    server.seek(2)
    handle2 = server._live_scene._handle_from_node_name["/frame"]
    assert tuple(handle2.position) == (2.0, 0.0, 0.0)


def test_seek_backwards_removes_late_adds(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        server.scene.add_frame("/a", axes_length=0.1)
    with server.at(1):
        server.scene.add_frame("/b", axes_length=0.1)

    server.seek(1)
    handles = server._live_scene._handle_from_node_name
    assert "/a" in handles
    assert "/b" in handles

    server.seek(0)
    handles = server._live_scene._handle_from_node_name
    assert "/a" in handles
    assert "/b" not in handles


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

    server.seek(1)
    handle1 = server._live_scene._handle_from_node_name["/frame"]
    assert tuple(handle1.position) == (0.0, 0.0, 0.0)
    assert tuple(handle1.wxyz) == (1.0, 0.0, 0.0, 0.0)

    server.seek(2)
    handle2 = server._live_scene._handle_from_node_name["/frame"]
    assert tuple(handle2.position) == (2.0, 0.0, 0.0)
    assert tuple(handle2.wxyz) == (1.0, 0.0, 0.0, 0.0)


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
    handles = server._live_scene._handle_from_node_name
    assert tuple(handles["/a"].position) == (0.0, 0.0, 0.0)
    assert tuple(handles["/b"].position) == (1.0, 0.0, 0.0)

    server.seek(2)
    handles = server._live_scene._handle_from_node_name
    assert tuple(handles["/a"].position) == (2.0, 0.0, 0.0)
    assert tuple(handles["/b"].position) == (2.0, 0.0, 0.0)


def test_remove_by_name_is_recorded(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        server.scene.add_frame("/a", axes_length=0.1)
    with server.at(1):
        server.scene.remove_by_name("/a")
    with server.at(2):
        a = server.scene.add_frame("/a", axes_length=0.1)
        a.position = (2.0, 0.0, 0.0)

    server.seek(0)
    assert "/a" in server._live_scene._handle_from_node_name

    server.seek(1)
    assert "/a" not in server._live_scene._handle_from_node_name

    server.seek(2)
    handle = server._live_scene._handle_from_node_name["/a"]
    assert tuple(handle.position) == (2.0, 0.0, 0.0)


def test_handle_remove_is_recorded(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        handle = server.scene.add_frame("/a", axes_length=0.1)
    with server.at(1):
        handle.remove()
    with server.at(2):
        server.scene.add_frame("/a", axes_length=0.1)

    server.seek(0)
    assert "/a" in server._live_scene._handle_from_node_name

    server.seek(1)
    assert "/a" not in server._live_scene._handle_from_node_name

    server.seek(2)
    assert "/a" in server._live_scene._handle_from_node_name


def test_play_loops_by_default(server: viser4d.Viser4dServer) -> None:
    """play() defaults to looping playback."""
    captured: list[bool] = []

    def fake_start(loop: bool) -> None:
        captured.append(loop)

    server._start_playback = fake_start  # type: ignore[method-assign]
    server.play(fps=30)
    asyncio.run_coroutine_threadsafe(asyncio.sleep(0), server.get_event_loop()).result(
        timeout=1
    )

    assert captured == [True]


def test_on_timestep_change_callback(server: viser4d.Viser4dServer) -> None:
    """Timestep callbacks are invoked on seek."""
    called_with: list[int] = []

    def callback(t: int) -> None:
        called_with.append(t)

    server.on_timestep_change(callback)

    server.seek(0)
    server.seek(2)
    server.seek(1)

    assert called_with == [0, 2, 1]


def test_multiple_timestep_callbacks(server: viser4d.Viser4dServer) -> None:
    """Multiple callbacks are all invoked."""
    results: list[str] = []

    server.on_timestep_change(lambda t: results.append(f"a:{t}"))
    server.on_timestep_change(lambda t: results.append(f"b:{t}"))

    server.seek(1)

    assert results == ["a:1", "b:1"]


def test_current_time_property(server: viser4d.Viser4dServer) -> None:
    """current_time reflects the current timestep."""
    assert server.current_time == 0

    server.seek(2)
    assert server.current_time == 2

    server.seek(1)
    assert server.current_time == 1


def test_handles_property(server: viser4d.Viser4dServer) -> None:
    """handles property returns all recorded handle names."""
    with server.at(0):
        server.scene.add_frame("/a", axes_length=0.1)
        server.scene.add_frame("/b", axes_length=0.1)
    with server.at(1):
        server.scene.add_frame("/c", axes_length=0.1)

    assert set(server.handles) == {"/a", "/b", "/c"}


def test_get_handle_returns_proxy(server: viser4d.Viser4dServer) -> None:
    """get_handle returns a ProxyHandle for the given name."""
    with server.at(0):
        server.scene.add_frame("/frame", axes_length=0.1)

    handle = server.get_handle("/frame")
    assert handle._name == "/frame"


def test_proxy_handle_live_read(server: viser4d.Viser4dServer) -> None:
    """ProxyHandle reads attributes from live handle when outside at()."""
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (1.0, 2.0, 3.0)

    server.seek(0)

    # Read from live handle via proxy
    assert tuple(handle.position) == (1.0, 2.0, 3.0)


def test_proxy_handle_live_write(server: viser4d.Viser4dServer) -> None:
    """ProxyHandle writes to live handle when outside at()."""
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (0.0, 0.0, 0.0)

    server.seek(0)

    # Write to live handle via proxy (outside at() context)
    handle.position = (5.0, 5.0, 5.0)

    # Verify change went to live scene
    live_handle = server._live_scene._handle_from_node_name["/frame"]
    assert tuple(live_handle.position) == (5.0, 5.0, 5.0)


def test_proxy_handle_live_write_persists_across_seek(
    server: viser4d.Viser4dServer,
) -> None:
    """Live writes persist across seeks (useful for runtime visibility toggles)."""
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (0.0, 0.0, 0.0)

    server.seek(0)
    handle.visible = False  # Live change (e.g., user toggled checkbox)

    # Seeking doesn't overwrite live changes (no recorded visibility change)
    server.seek(0)
    live_handle = server._live_scene._handle_from_node_name["/frame"]
    assert live_handle.visible is False


def test_recorded_change_overwrites_live_change(server: viser4d.Viser4dServer) -> None:
    """Recorded timeline changes do overwrite live changes when seeking."""
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (0.0, 0.0, 0.0)
    with server.at(1):
        handle.position = (1.0, 1.0, 1.0)

    server.seek(0)
    handle.position = (5.0, 5.0, 5.0)  # Live change

    # Seeking to t=1 applies recorded state, overwriting live change
    server.seek(1)
    live_handle = server._live_scene._handle_from_node_name["/frame"]
    assert tuple(live_handle.position) == (1.0, 1.0, 1.0)


def test_get_handle_for_bulk_operations(server: viser4d.Viser4dServer) -> None:
    """get_handle enables bulk operations by name pattern."""
    with server.at(0):
        server.scene.add_frame("/group/a", axes_length=0.1)
        server.scene.add_frame("/group/b", axes_length=0.1)
        server.scene.add_frame("/other", axes_length=0.1)

    server.seek(0)

    # Bulk toggle visibility for /group/* handles
    for name in server.handles:
        if name.startswith("/group/"):
            server.get_handle(name).visible = False

    # Verify visibility
    assert server._live_scene._handle_from_node_name["/group/a"].visible is False
    assert server._live_scene._handle_from_node_name["/group/b"].visible is False
    assert server._live_scene._handle_from_node_name["/other"].visible is True


def test_proxy_handle_error_before_seek(server: viser4d.Viser4dServer) -> None:
    """ProxyHandle raises clear error when accessed before seek."""
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)

    # Try to access before seek - should raise
    import pytest

    with pytest.raises(RuntimeError, match="not in live scene"):
        _ = handle.position


def test_at_context_is_thread_local(server: viser4d.Viser4dServer) -> None:
    """Each thread sees its own recording timestep inside at()."""
    barrier = threading.Barrier(2)

    def observe_time(t: int) -> int | None:
        with server.at(t):
            barrier.wait(timeout=1.0)
            return server.scene._recording_time

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(observe_time, 0)
        future_b = pool.submit(observe_time, 2)

    seen = {future_a.result(), future_b.result()}
    assert seen == {0, 2}
