"""Single-point adapter for viser's private APIs.

Every underscore-prefixed import from viser is concentrated here so that
upstream internal changes only require updating this one file.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import viser
from viser import _messages
from viser._scene_api import SceneApi
from viser._viser import ClientHandle as ClientHandle
from viser.infra import WebsockMessageHandler as WebsockMessageHandler

# Re-exported types – other modules import these instead of reaching into
# viser internals directly.
Message = _messages.Message


def run_javascript_message(source: str) -> Message:
    return _messages.RunJavascriptMessage(source)


def create_scene_api(
    owner: object,
    *,
    thread_executor: ThreadPoolExecutor,
    event_loop: asyncio.AbstractEventLoop,
) -> SceneApi:
    return SceneApi(
        owner,  # type: ignore[arg-type]
        thread_executor=thread_executor,
        event_loop=event_loop,
    )


def gui_uuid(handle: Any) -> str:
    return handle._impl.uuid


def set_scene_owner(scene: SceneApi, owner: object) -> None:
    scene._owner = owner


def scene_has_node(scene: SceneApi, name: str) -> bool:
    return name in scene._handle_from_node_name


def broadcast_messages(server: viser.ViserServer) -> list[Message]:
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


def queue_server_message(server: viser.ViserServer, message: Message) -> None:
    server._websock_server.queue_message(message)


def queue_client_message(client: Any, message: Message) -> None:
    client._websock_connection.queue_message(message)


def register_message_handler(
    server: viser.ViserServer,
    message_cls: type[Any],
    callback: Callable[..., Any],
) -> None:
    server._websock_server.register_handler(message_cls, callback)


def unregister_message_handler(
    server: viser.ViserServer,
    message_cls: type[Any],
    callback: Any = None,
) -> None:
    server._websock_server.unregister_handler(message_cls, callback)


def server_thread_executor(server: viser.ViserServer) -> ThreadPoolExecutor:
    return server._thread_executor


def is_create_scene_node_message(message: object) -> bool:
    return isinstance(message, _messages._CreateSceneNodeMessage)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.removeprefix("#")
    return (int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16))
