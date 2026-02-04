"""Integration tests for lazy loading of heavy Op payloads."""

from collections.abc import Iterator

import numpy as np
import pytest

import viser4d
from viser4d.op import Op, OpKind, _get_cache_dir


@pytest.fixture
def server() -> Iterator[viser4d.ViserServer]:
    """Server with a very low lazy threshold (100 bytes) to trigger lazy loading."""
    server = viser4d.ViserServer(
        num_steps=3,
        host="127.0.0.1",
        port=0,
        verbose=False,
        enable_playback_gui=False,
        lazy_threshold_bytes=100,  # Very low threshold for testing
    )
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def server_high_threshold() -> Iterator[viser4d.ViserServer]:
    """Server with a very high lazy threshold to keep everything eager."""
    server = viser4d.ViserServer(
        num_steps=3,
        host="127.0.0.1",
        port=0,
        verbose=False,
        enable_playback_gui=False,
        lazy_threshold_bytes=1024 * 1024 * 1024,  # 1GB - nothing will be lazy
    )
    try:
        yield server
    finally:
        server.stop()


# =============================================================================
# Threshold selection tests
# =============================================================================


def test_small_payload_uses_eager() -> None:
    """Small data stays in memory."""
    op = Op.create(
        kind=OpKind.ADD,
        target="/test",
        member="add_frame",
        args=("/test",),
        kwargs={"axes_length": 0.1},
        threshold_bytes=1024 * 1024,  # 1MB
    )
    assert not op.is_lazy()


def test_large_payload_uses_lazy() -> None:
    """Large data goes to disk."""
    large_array = np.zeros((1000, 1000), dtype=np.float64)  # ~8MB
    op = Op.create(
        kind=OpKind.ADD,
        target="/test",
        member="add_point_cloud",
        args=("/test", large_array),
        threshold_bytes=1024 * 1024,  # 1MB
    )
    assert op.is_lazy()


def test_threshold_is_respected() -> None:
    """Threshold parameter controls eager vs lazy selection."""
    # 1KB array
    array = np.zeros((128,), dtype=np.float64)  # ~1KB

    # With 100 byte threshold -> lazy
    op_lazy = Op.create(
        kind=OpKind.ADD,
        target="/test",
        member="add_point_cloud",
        args=("/test", array),
        threshold_bytes=100,
    )
    assert op_lazy.is_lazy()

    # With 1MB threshold -> eager
    op_eager = Op.create(
        kind=OpKind.ADD,
        target="/test",
        member="add_point_cloud",
        args=("/test", array),
        threshold_bytes=1024 * 1024,
    )
    assert not op_eager.is_lazy()


# =============================================================================
# Round-trip tests
# =============================================================================


def test_array_survives_roundtrip() -> None:
    """Numpy array data is preserved after lazy load."""
    original = np.array([[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
    op = Op.create(
        kind=OpKind.ADD,
        target="/test",
        member="add_point_cloud",
        args=("/test", original),
        threshold_bytes=0,  # Force lazy
    )
    assert op.is_lazy()

    # Access triggers load from disk
    loaded = op.args[1]
    np.testing.assert_array_equal(loaded, original)


def test_kwargs_survive_roundtrip() -> None:
    """Keyword arguments are preserved after lazy load."""
    original_points = np.array([[1.0, 2.0, 3.0]])
    original_colors = np.array([[255, 0, 0]], dtype=np.uint8)

    op = Op.create(
        kind=OpKind.ADD,
        target="/test",
        member="add_point_cloud",
        args=("/test",),
        kwargs={"points": original_points, "colors": original_colors},
        threshold_bytes=0,  # Force lazy
    )
    assert op.is_lazy()

    np.testing.assert_array_equal(op.kwargs["points"], original_points)
    np.testing.assert_array_equal(op.kwargs["colors"], original_colors)


def test_mixed_types_survive_roundtrip() -> None:
    """Mix of arrays, strings, numbers survive round-trip."""
    original_array = np.array([1.0, 2.0, 3.0])

    op = Op.create(
        kind=OpKind.ADD,
        target="/test",
        member="add_point_cloud",
        args=("/test", original_array),
        kwargs={"scale": 2.5, "visible": True, "label": "test"},
        threshold_bytes=0,  # Force lazy
    )
    assert op.is_lazy()

    assert op.args[0] == "/test"
    np.testing.assert_array_equal(op.args[1], original_array)
    assert op.kwargs["scale"] == 2.5
    assert op.kwargs["visible"] is True
    assert op.kwargs["label"] == "test"


# =============================================================================
# Disk storage tests
# =============================================================================


def test_lazy_payload_creates_file() -> None:
    """Lazy payload creates a pickle file on disk."""
    op = Op.create(
        kind=OpKind.ADD,
        target="/test",
        member="add_frame",
        args=("/test",),
        threshold_bytes=0,  # Force lazy
    )
    assert op.is_lazy()

    # Check that cache directory exists and has files
    cache_dir = _get_cache_dir()
    assert cache_dir.exists()
    pkl_files = list(cache_dir.glob("*.pkl"))
    assert len(pkl_files) >= 1


# =============================================================================
# Server integration tests
# =============================================================================


def test_server_uses_configured_threshold(server: viser4d.ViserServer) -> None:
    """Server passes threshold to Op creation."""
    # Server fixture has 100 byte threshold
    with server.at(0):
        # Even small frame should be lazy with 100 byte threshold
        server.scene.add_frame("/frame", axes_length=0.1)

    # Check timeline has a lazy op
    series = server._timeline._adds.get("/frame")
    assert series is not None
    op = series.values[0]
    assert op.is_lazy()


def test_server_high_threshold_uses_eager(
    server_high_threshold: viser4d.ViserServer,
) -> None:
    """Server with high threshold keeps data eager."""
    with server_high_threshold.at(0):
        server_high_threshold.scene.add_frame("/frame", axes_length=0.1)

    series = server_high_threshold._timeline._adds.get("/frame")
    assert series is not None
    op = series.values[0]
    assert not op.is_lazy()


def test_lazy_data_renders_correctly(server: viser4d.ViserServer) -> None:
    """Lazy-loaded data renders correctly during seek."""
    with server.at(0):
        handle = server.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (1.0, 2.0, 3.0)

    with server.at(1):
        handle.position = (4.0, 5.0, 6.0)

    # Seek should load lazy data and apply correctly
    server.seek(0)
    live_handle = server._live_scene._handle_from_node_name["/frame"]
    assert tuple(live_handle.position) == (1.0, 2.0, 3.0)

    server.seek(1)
    live_handle = server._live_scene._handle_from_node_name["/frame"]
    assert tuple(live_handle.position) == (4.0, 5.0, 6.0)


# =============================================================================
# LRU cache tests
# =============================================================================


def test_cache_returns_same_data() -> None:
    """Accessing the same Op multiple times returns consistent data."""
    original = np.array([1.0, 2.0, 3.0])
    op = Op.create(
        kind=OpKind.ADD,
        target="/test",
        member="add_point_cloud",
        args=("/test", original),
        threshold_bytes=0,  # Force lazy
    )

    # Multiple accesses should return equal data
    first_access = op.args[1]
    second_access = op.args[1]
    np.testing.assert_array_equal(first_access, original)
    np.testing.assert_array_equal(second_access, original)
