from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

import numpy as np
from viser import _messages
from viser._scene_api import SceneApi
from viser.infra import WebsockMessageHandler

from .. import _viser_private as impl
from ..audio._api import AudioHandle, AudioState, audio_array_payload
from ..audio._messages import AddAudioMessage
from .._runtime import make_runtime_message
from .._types import StoredMessage
from ._store import (
    TimelineStore,
    serialize_stored_message,
    serialize_stored_messages,
    store_raw_message,
    store_raw_messages,
)

if TYPE_CHECKING:
    from ..audio import AudioApi
    from .._server import Viser4dServer
    from viser._viser import ClientHandle


@dataclass(frozen=True)
class TimelineContext:
    scene: SceneApi
    audio: AudioApi


class SceneRecorder:
    """Capture per-timestep scene and audio edits from a timeline-only scene."""

    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._timeline = timeline
        self._active_step: int | None = None
        self._pending_messages: list[_messages.Message] | None = None
        self._transport = _TimelineTransport(server, self)
        owner = _TimelineSceneOwner(self._transport)
        # SceneApi only reads the client-like fields defined on _TimelineSceneOwner.
        self.scene = SceneApi(
            owner,  # type: ignore[arg-type]
            thread_executor=server._thread_executor,
            event_loop=server.get_event_loop(),
        )
        self.scene._owner = server
        self._transport.start()

    @property
    def active_step(self) -> int | None:
        return self._active_step

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[TimelineContext]:
        """Expose the timeline-only scene and audio APIs for timestep ``t``."""
        if self._active_step is not None:
            raise RuntimeError("Nested server.at(t) blocks are not supported.")
        step = self._timeline.validate_step(t)
        self._active_step = step
        self._pending_messages = []
        try:
            yield TimelineContext(
                scene=self.scene,
                audio=self._server.audio,
            )
        finally:
            self._active_step = None
            pending_messages = self._pending_messages
            self._pending_messages = None

        if not pending_messages:
            return

        self._record_and_preload(step, store_raw_messages(pending_messages))

    def add_audio(
        self,
        name: str,
        *,
        data: np.ndarray,
        sample_rate: int,
    ) -> AudioHandle:
        """Create a timeline-owned audio track for the active timestep."""
        state = AudioState(
            name=name,
            sample_rate=sample_rate,
            waveform=np.ascontiguousarray(data),
        )
        handle = AudioHandle(self._server, state)
        assert self._active_step is not None
        message = AddAudioMessage(
            name=name,
            sampleRate=state.sample_rate,
            waveform=audio_array_payload(state.waveform),
            volume=state.volume,
        )
        stored_messages = store_raw_messages([message])
        self._record_and_preload(self._active_step, stored_messages)
        return handle

    def dispatch_audio_update(self, message: _messages.Message) -> None:
        """Route audio updates either into the active step or directly to clients."""
        if self._active_step is not None:
            stored_messages = store_raw_messages([message])
            self._record_and_preload(self._active_step, stored_messages)
            return
        self._send_runtime_update(message)

    def _record_and_preload(
        self, step: int, stored_messages: list[StoredMessage]
    ) -> None:
        """Store one timestep and preload its serialized updates into live runtimes."""
        step_state = self._timeline.record_step(step, stored_messages)
        payload = {
            "step": step,
            "messages": serialize_stored_messages(stored_messages),
            "nodeNames": sorted(step_state.node_names),
        }
        self._server._send_runtime_call("preloadStep", payload)

    def _push_timeline_message(self, message: _messages.Message) -> None:
        if self._active_step is None or self._pending_messages is None:
            self._push_live_message(message)
            return
        self._validate_create_message(message)
        self._pending_messages.append(message)

    def _push_live_message(self, message: _messages.Message) -> None:
        if isinstance(message, _messages._CreateSceneNodeMessage):
            raise RuntimeError(
                "Timeline scene nodes can only be created inside server.at(t)."
            )
        self._broadcast_live_scene_update(message)

    def _send_runtime_update(self, message: _messages.Message) -> None:
        stored_message = store_raw_message(message)
        self._server._send_runtime_call(
            "applyMessageUpdate",
            {
                "message": serialize_stored_message(stored_message),
            },
        )

    def sync_client_scene_overlays(self, client: ClientHandle) -> None:
        """Prime one client's runtime cache with the latest live scene overlays."""
        for redundancy_key, message in self._timeline.iter_live_scene_updates():
            impl.queue_client_message(
                client,
                make_runtime_message(
                    "cacheSceneOverlay",
                    {
                        "message": serialize_stored_message(message),
                        "redundancyKey": redundancy_key,
                        "clearNodeName": (
                            message.get("name")
                            if message.get("type") == "RemoveSceneNodeMessage"
                            else None
                        ),
                    },
                ),
            )

    def _broadcast_live_scene_update(self, message: _messages.Message) -> None:
        stored_message = store_raw_message(message)
        redundancy_key = message.redundancy_key()
        self._timeline.record_live_scene_update(
            stored_message,
            redundancy_key=redundancy_key,
        )
        runtime_message = make_runtime_message(
            "cacheSceneOverlay",
            {
                "message": serialize_stored_message(stored_message),
                "redundancyKey": redundancy_key,
                "clearNodeName": (
                    message.name
                    if isinstance(message, _messages.RemoveSceneNodeMessage)
                    else None
                ),
            },
        )
        excluded_client_id = message.excluded_self_client
        for client in self._server.get_clients().values():
            impl.queue_client_message(client, runtime_message)
            if excluded_client_id == client.client_id:
                continue
            impl.queue_client_message(client, message)

    def _validate_create_message(self, message: _messages.Message) -> None:
        if not isinstance(message, _messages._CreateSceneNodeMessage):
            return
        name = message.name
        if name in self._server.scene._handle_from_node_name:
            raise RuntimeError(
                f"Cannot create timeline node {name!r} because a static scene node "
                "with the same name already exists."
            )
        if name in self.scene._handle_from_node_name or self._timeline.has_node(name):
            raise RuntimeError(
                f"Cannot create timeline node {name!r} more than once. "
                "Create it once and update the returned handle inside later "
                "server.at(t) blocks."
            )


class _TimelineTransport(WebsockMessageHandler):
    def __init__(self, server: Viser4dServer, recorder: SceneRecorder) -> None:
        super().__init__()
        self._server = server
        self._recorder = recorder
        self._recording_enabled = False

    def start(self) -> None:
        self._recording_enabled = True

    def get_message_buffer(self) -> Any:
        return self

    def push(self, message: _messages.Message) -> None:
        if not self._recording_enabled:
            return
        self._recorder._push_timeline_message(message)

    def atomic_start(self) -> None:
        pass

    def atomic_end(self) -> None:
        pass

    def register_handler(self, message_cls: type[Any], callback: Any) -> None:
        self._server._websock_server.register_handler(message_cls, callback)

    def unregister_handler(self, message_cls: type[Any], callback: Any = None) -> None:
        self._server._websock_server.unregister_handler(message_cls, callback)


class _TimelineSceneOwner:
    """Minimal client-like owner required by ``SceneApi``."""

    def __init__(self, transport: _TimelineTransport) -> None:
        self._websock_connection = transport
