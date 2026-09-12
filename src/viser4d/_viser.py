"""Single-point adapter for viser's private APIs.

viser has no extension API, so viser4d necessarily reaches into a few of its
internals. Every such access is concentrated here; the rest of the package
imports only this module. Target viser: ``>=1.1.0,<1.2``.

Python-side coupling inventory (everything below is private viser API):

- ``viser._messages``: ``Message``, ``RunJavascriptMessage``,
  ``_CreateSceneNodeMessage`` (isinstance),
  ``Message.as_serializable_dict(binary_buffers=...)``
- ``viser._scene_api.SceneApi(owner, thread_executor=, event_loop=)``,
  ``SceneApi._owner``, ``SceneApi._handle_from_node_name``
- ``server._websock_server``: ``queue_message``, ``register_handler``,
  ``unregister_handler``; ``client._websock_connection.queue_message``
- ``viser.infra.WebsockMessageHandler`` as the shadow-transport base
- ``StateSerializer._messages`` / ``._time`` (no public
  equivalent for appending pre-serialized messages at chosen timestamps)

The browser side has its own inventory, confined to ``client/viser.ts``.
Every wire ``Message`` subclass must declare
``include_in_scene_serialization=False`` so viser does not replay control
traffic into exported recordings.
"""

from __future__ import annotations

import types
import typing
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import viser
from viser import _messages, infra
from viser._scene_api import SceneApi
from viser._viser import ClientHandle

Message = _messages.Message
WebsockMessageHandler = infra.WebsockMessageHandler


def run_javascript_message(source: str) -> Message:
    return _messages.RunJavascriptMessage(source)


def is_create_scene_node_message(message: object) -> bool:
    return isinstance(message, _messages._CreateSceneNodeMessage)


def create_scene_api(
    server: viser.ViserServer, transport: WebsockMessageHandler
) -> SceneApi:
    """Give the recording scene broadcast ownership and a separate transport."""
    owner = types.SimpleNamespace(
        _websock_connection=transport, client_id="", _viser_server=server
    )
    scene = SceneApi(
        typing.cast(Any, owner),
        thread_executor=server._thread_executor,
        event_loop=server.get_event_loop(),
    )
    scene._owner = server
    return scene


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


def append_serializer_message(serializer: Any, message: object) -> None:
    serializer._messages.append((serializer._time, message))
