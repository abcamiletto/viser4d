from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, cast

import viser
from viser import _messages


def gui_uuid(handle: Any) -> str:
    return handle._impl.uuid


def scene_owner(scene: viser.SceneApi) -> viser.ViserServer:
    return cast(viser.ViserServer, scene._owner)


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


def brand_color(
    server: viser.ViserServer,
) -> tuple[int, int, int] | tuple[str, ...] | None:
    for message in reversed(broadcast_messages(server)):
        if isinstance(message, _messages.ThemeConfigurationMessage):
            return message.colors
    return None


def queue_server_message(server: viser.ViserServer, message: _messages.Message) -> None:
    server._websock_server.queue_message(message)


def queue_client_message(client: Any, message: _messages.Message) -> None:
    client._websock_connection.queue_message(message)
