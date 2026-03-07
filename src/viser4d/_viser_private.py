from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator

import viser
from viser import _messages


def gui_uuid(handle: Any) -> str:
    return handle._impl.uuid


@contextmanager
def scene_recording_interface(
    scene: viser.SceneApi,
    recorder: Any,
) -> Iterator[None]:
    original_interface = scene._websock_interface
    scene._websock_interface = recorder
    try:
        yield
    finally:
        scene._websock_interface = original_interface


def broadcast_messages(server: viser.ViserServer) -> list[_messages.Message]:
    return [
        message
        for message in server._websock_server._broadcast_buffer.message_from_id.values()
        if isinstance(message, _messages.Message)
    ]
