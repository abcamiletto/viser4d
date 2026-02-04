from collections.abc import Iterator

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
