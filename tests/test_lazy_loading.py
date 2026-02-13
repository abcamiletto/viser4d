"""Black-box integration tests for lazy configuration options."""

from collections.abc import Iterator

import numpy as np
import pytest

import viser4d


@pytest.fixture
def server_low_threshold() -> Iterator[viser4d.Viser4dServer]:
    server = viser4d.Viser4dServer(
        num_steps=3,
        host="127.0.0.1",
        port=0,
        verbose=False,
        enable_playback_gui=False,
        lazy_threshold_bytes=100,
    )
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def server_high_threshold() -> Iterator[viser4d.Viser4dServer]:
    server = viser4d.Viser4dServer(
        num_steps=3,
        host="127.0.0.1",
        port=0,
        verbose=False,
        enable_playback_gui=False,
        lazy_threshold_bytes=1024 * 1024 * 1024,
    )
    try:
        yield server
    finally:
        server.stop()


def test_low_threshold_preserves_recorded_motion(
    server_low_threshold: viser4d.Viser4dServer,
) -> None:
    with server_low_threshold.at(0):
        handle = server_low_threshold.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (1.0, 2.0, 3.0)
    with server_low_threshold.at(1):
        handle.position = (4.0, 5.0, 6.0)

    server_low_threshold.seek(0)
    assert tuple(handle.position) == (1.0, 2.0, 3.0)

    server_low_threshold.seek(1)
    assert tuple(handle.position) == (4.0, 5.0, 6.0)


def test_high_threshold_preserves_recorded_motion(
    server_high_threshold: viser4d.Viser4dServer,
) -> None:
    with server_high_threshold.at(0):
        handle = server_high_threshold.scene.add_frame("/frame", axes_length=0.1)
        handle.position = (1.0, 0.0, 0.0)
    with server_high_threshold.at(2):
        handle.position = (2.0, 0.0, 0.0)

    server_high_threshold.seek(0)
    assert tuple(handle.position) == (1.0, 0.0, 0.0)

    server_high_threshold.seek(2)
    assert tuple(handle.position) == (2.0, 0.0, 0.0)


def test_large_point_cloud_roundtrip_with_low_threshold(
    server_low_threshold: viser4d.Viser4dServer,
) -> None:
    points0 = np.zeros((2000, 3), dtype=np.float32)
    points1 = np.ones((2000, 3), dtype=np.float32)

    with server_low_threshold.at(0):
        handle = server_low_threshold.scene.add_point_cloud(
            "/points",
            points=points0,
            colors=(255, 100, 0),
        )
    with server_low_threshold.at(1):
        handle.points = points1

    server_low_threshold.seek(0)
    np.testing.assert_array_equal(handle.points, points0)

    server_low_threshold.seek(1)
    np.testing.assert_array_equal(handle.points, points1)


@pytest.mark.parametrize("mode", list(viser4d.CompressionMode))
def test_all_compression_modes_work(mode: viser4d.CompressionMode) -> None:
    server = viser4d.Viser4dServer(
        num_steps=2,
        host="127.0.0.1",
        port=0,
        verbose=False,
        enable_playback_gui=False,
        lazy_threshold_bytes=0,
        compression=mode,
    )
    try:
        with server.at(0):
            handle = server.scene.add_frame("/frame", axes_length=0.1)
            handle.position = (0.0, 0.0, 0.0)
        with server.at(1):
            handle.position = (1.0, 0.0, 0.0)

        server.seek(1)
        assert tuple(handle.position) == (1.0, 0.0, 0.0)
    finally:
        server.stop()
