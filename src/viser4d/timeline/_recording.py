from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterable, Iterator

import numpy as np
from viser import _messages
from viser._scene_api import SceneApi
from viser.infra import WebsockMessageHandler

from .. import _viser_private as impl
from ..audio._api import AudioHandle, AudioState, audio_array_payload
from ..audio._messages import AddAudioMessage, is_audio_message_type
from .._runtime import make_runtime_message
from .._types import StoredMessage
from ._store import (
    TimelineStore,
    serialize_stored_message,
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

        self._record_and_preload(step, pending_messages)

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
        self._record_and_preload(self._active_step, [message])
        return handle

    def dispatch_audio_update(self, message: _messages.Message) -> None:
        """Route audio updates either into the active step or directly to clients."""
        if self._active_step is not None:
            self._record_and_preload(self._active_step, [message])
            return
        self._send_runtime_update(message)

    def _record_and_preload(
        self,
        step: int,
        raw_messages: list[_messages.Message],
    ) -> None:
        """Store one timestep and preload its serialized updates into live runtimes."""
        stored_messages = store_raw_messages(raw_messages)
        step_state = self._timeline.record_step(step, raw_messages, stored_messages)
        scene_messages: list[tuple[str, StoredMessage]] = []
        audio_messages: list[StoredMessage] = []
        for raw_message, stored_message in zip(raw_messages, stored_messages):
            if is_audio_message_type(stored_message.get("type")):
                audio_messages.append(stored_message)
                continue
            scene_messages.append((raw_message.redundancy_key(), stored_message))
        preload_message = self._make_preload_step_message(
            step,
            scene_messages=scene_messages,
            audio_messages=audio_messages,
            node_names=step_state.node_names,
        )
        for client in self._server.get_clients().values():
            impl.queue_client_message(client, preload_message)

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

    def sync_client_timeline(self, client: ClientHandle) -> None:
        """Send the current timeline cache to one newly connected client."""
        for step_index, step_state in enumerate(self._timeline.steps):
            if not step_state.scene_updates and not step_state.audio_updates:
                continue
            impl.queue_client_message(
                client,
                self._make_preload_step_message(
                    step_index,
                    scene_messages=step_state.scene_updates.items(),
                    audio_messages=step_state.audio_updates,
                    node_names=step_state.node_names,
                ),
            )

    def _broadcast_live_scene_update(self, message: _messages.Message) -> None:
        stored_message = store_raw_message(message)
        redundancy_key = message.redundancy_key()
        start_step = self._timeline.record_live_scene_update(
            stored_message,
            name=getattr(message, "name", None),
            redundancy_key=redundancy_key,
        )
        preload_message = self._make_preload_step_message(
            start_step,
            scene_messages=((redundancy_key, stored_message),),
        )
        for client in self._server.get_clients().values():
            impl.queue_client_message(client, preload_message)
            if message.excluded_self_client == client.client_id:
                continue
            playback = self._server.get_client_playback(client.client_id)
            if playback is None or playback.current_timestep < start_step:
                continue
            impl.queue_client_message(client, message)

    def _make_preload_step_message(
        self,
        step: int,
        *,
        scene_messages: Iterable[tuple[str, StoredMessage]],
        audio_messages: Iterable[StoredMessage] = (),
        node_names: set[str] | None = None,
    ) -> _messages.RunJavascriptMessage:
        return make_runtime_message(
            "preloadStep",
            {
                "step": step,
                "sceneMessages": [
                    {
                        "redundancyKey": redundancy_key,
                        "message": serialize_stored_message(message),
                    }
                    for redundancy_key, message in scene_messages
                ],
                "audioMessages": [
                    serialize_stored_message(message) for message in audio_messages
                ],
                "nodeNames": [] if node_names is None else sorted(node_names),
            },
        )

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
