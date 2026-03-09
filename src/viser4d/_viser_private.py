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


def playback_brand_color(
    server: viser.ViserServer,
) -> tuple[int, int, int] | None:
    for message in reversed(broadcast_messages(server)):
        if isinstance(message, _messages.ThemeConfigurationMessage):
            colors = message.colors
            if colors is None:
                return None
            return _hex_to_rgb(colors[8])
    return None


def queue_server_message(server: viser.ViserServer, message: _messages.Message) -> None:
    server._websock_server.queue_message(message)


def queue_client_message(client: Any, message: _messages.Message) -> None:
    client._websock_connection.queue_message(message)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.removeprefix("#")
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
