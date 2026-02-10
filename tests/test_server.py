from collections.abc import Iterator

import numpy as np
import pytest
import viser4d
from viser import _messages as _viser_messages


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

    def fake_loop(loop: bool) -> None:
        captured.append(loop)

    server._playback_loop = fake_loop  # type: ignore[method-assign]
    server.play(fps=30)
    thread = server._playback_thread
    assert thread is not None
    thread.join(timeout=1)

    assert captured == [True]


def test_play_sets_gui_state_when_started_programmatically(
    server: viser4d.Viser4dServer,
) -> None:
    states: list[bool] = []

    class _FakeControls:
        def set_playing(self, playing: bool) -> None:
            states.append(playing)

        def set_fps(self, fps: float) -> None:
            return

    server._playback_controls = _FakeControls()  # type: ignore[assignment]
    server._playback_loop = lambda loop: None  # type: ignore[method-assign]

    server.play(fps=30)
    thread = server._playback_thread
    assert thread is not None
    thread.join(timeout=1)

    assert states == [True]


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


def test_add_audio_requires_at_context(server: viser4d.Viser4dServer) -> None:
    audio = np.zeros(128, dtype=np.float32)

    with pytest.raises(RuntimeError, match="inside `with server.at\\(t\\):`"):
        server.scene.add_audio(audio, sample_rate=16_000)


def test_add_audio_rejects_invalid_dtype(server: viser4d.Viser4dServer) -> None:
    with server.at(0):
        with pytest.raises(TypeError, match="int16 or float32"):
            server.scene.add_audio(np.zeros(128, dtype=np.float64), sample_rate=16_000)


def test_seek_dispatches_audio_message(server: viser4d.Viser4dServer) -> None:
    class _FakeConnection:
        def __init__(self) -> None:
            self.messages: list[_viser_messages.RunJavascriptMessage] = []

        def queue_message(self, message: _viser_messages.RunJavascriptMessage) -> None:
            self.messages.append(message)

    class _FakeClient:
        def __init__(self) -> None:
            self._websock_connection = _FakeConnection()

        def flush(self) -> None:
            return

    fake_client = _FakeClient()
    server.get_clients = lambda: {0: fake_client}  # type: ignore[method-assign]

    with server.at(1):
        server.scene.add_audio(np.zeros(64, dtype=np.int16), sample_rate=8_000)

    server.seek(0)
    assert fake_client._websock_connection.messages == []

    server.seek(1)
    messages = fake_client._websock_connection.messages
    assert len(messages) == 1
    assert isinstance(messages[0], _viser_messages.RunJavascriptMessage)
    assert "data:audio/wav;base64," in messages[0].source
