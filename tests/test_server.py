from collections.abc import Iterator

import pytest
import viser4d


@pytest.fixture
def server() -> Iterator[viser4d.ViserServer]:
    server = viser4d.ViserServer(
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


def test_seek_applies_recorded_updates(server: viser4d.ViserServer) -> None:
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


def test_seek_backwards_removes_late_adds(server: viser4d.ViserServer) -> None:
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


def test_seek_uses_latest_attribute_value(server: viser4d.ViserServer) -> None:
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


def test_seek_multiple_objects(server: viser4d.ViserServer) -> None:
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


def test_remove_by_name_is_recorded(server: viser4d.ViserServer) -> None:
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


def test_handle_remove_is_recorded(server: viser4d.ViserServer) -> None:
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
