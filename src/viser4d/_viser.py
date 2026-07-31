"""Single-point adapter for viser's private APIs.

viser has no extension API, so viser4d necessarily reaches into a few of its
internals. Every such access is concentrated here; the rest of the package
imports only this module. See ARCHITECTURE.md for the full coupling inventory.
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

Message = _messages.Message


def run_javascript_message(source: str) -> Message:
    return _messages.RunJavascriptMessage(source)


def is_create_scene_node_message(message: object) -> bool:
    return isinstance(message, _messages._CreateSceneNodeMessage)


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


def set_scene_owner(scene: SceneApi, owner: object) -> None:
    scene._owner = owner


def scene_has_node(scene: SceneApi, name: str) -> bool:
    return name in scene._handle_from_node_name


def queue_server_message(server: viser.ViserServer, message: Message) -> None:
    server._websock_server.queue_message(message)


def queue_client_message(client: ClientHandle, message: Message) -> None:
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


def serializer_binary_buffers(serializer: Any) -> list[memoryview]:
    return serializer._binary_buffers


def append_serializer_message(serializer: Any, message: object) -> None:
    serializer._messages.append((serializer._time, message))
