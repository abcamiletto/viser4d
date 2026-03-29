from __future__ import annotations

import contextlib
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator

import numpy as np
from viser import _messages
from viser._scene_api import SceneApi
from viser.infra import WebsockMessageHandler

from .. import _viser_private as impl
from ..audio._api import AudioHandle, AudioState, audio_array_payload
from ..audio._messages import AddAudioMessage
from ._messages_util import serialize_stored_message, store_raw_message
from ._store import TimelineStore

if TYPE_CHECKING:
    from ..audio import AudioApi
    from .._server import Viser4dServer


@dataclass(frozen=True)
class TimelineContext:
    scene: SceneApi
    audio: AudioApi


class SceneRecorder:
    """Capture scene and audio edits into the timeline store."""

    _CLIENT_REFRESH_DELAY_SECONDS = 0.05

    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._live_scene = server.scene
        self._timeline = timeline
        self._active_step: int | None = None
        self._pending_messages: list[_messages.Message] | None = None
        self._pending_refresh_from_block: int | None = None
        self._refresh_timer: threading.Timer | None = None
        self._refresh_lock = threading.Lock()
        self._transport = _TimelineTransport(server, self)
        owner = _TimelineSceneOwner(self._transport)
        self.scene = SceneApi(
            owner,  # type: ignore[arg-type]
            thread_executor=server._thread_executor,
            event_loop=server.get_event_loop(),
        )
        # Reuse viser's existing server-owned client lookup for inbound events.
        self.scene._owner = server
        self._transport.start()

    @property
    def active_step(self) -> int | None:
        return self._active_step

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[TimelineContext]:
        if self._active_step is not None:
            raise RuntimeError("Nested server.at(t) blocks are not supported.")
        self._active_step = self._timeline.validate_step(t)
        self._pending_messages = []
        previous_scene = self._server.scene
        self._server.scene = self.scene
        try:
            yield TimelineContext(scene=self.scene, audio=self._server.audio)
        finally:
            self._server.scene = previous_scene
            pending_messages = self._pending_messages
            self._pending_messages = None
            active_step = self._active_step
            self._active_step = None

        if pending_messages is None or active_step is None or not pending_messages:
            return

        self._record_step(active_step, pending_messages)

    def add_audio(
        self,
        name: str,
        *,
        data: np.ndarray,
        sample_rate: int,
    ) -> AudioHandle:
        state = AudioState(
            name=name,
            sample_rate=sample_rate,
            waveform=np.ascontiguousarray(data),
        )
        handle = AudioHandle(self._server, state)
        assert self._active_step is not None
        self._record_step(
            self._active_step,
            [
                AddAudioMessage(
                    name=name,
                    sampleRate=state.sample_rate,
                    waveform=audio_array_payload(state.waveform),
                    volume=state.volume,
                )
            ],
        )
        return handle

    def dispatch_audio_update(self, message: _messages.Message) -> None:
        if self._active_step is not None:
            self._record_step(self._active_step, [message])
            return
        self._server._send_runtime_call(
            "applyMessageUpdate",
            serialize_stored_message(store_raw_message(message)),
        )

    def close(self) -> None:
        with self._refresh_lock:
            timer = self._refresh_timer
            self._refresh_timer = None
            self._pending_refresh_from_block = None
        if timer is not None:
            timer.cancel()

    def _record_step(self, step: int, messages: list[_messages.Message]) -> None:
        self._validate_step_messages(messages)
        self._timeline.record_step(step, messages)
        self._queue_client_block_refresh(self._timeline.block_index_for_step(step))

    def _record_live_scene_message(self, message: _messages.Message) -> None:
        if isinstance(message, _messages._CreateSceneNodeMessage):
            raise RuntimeError(
                "Timeline scene nodes can only be created inside server.at(t)."
            )
        start_step = self._timeline.record_live_scene_update(message)
        self._broadcast_scene_message(message, start_step)
        self._queue_client_block_refresh(
            self._timeline.block_index_for_step(start_step)
        )

    def _queue_client_block_refresh(self, changed_block: int) -> None:
        with self._refresh_lock:
            pending_block = self._pending_refresh_from_block
            if pending_block is None or changed_block < pending_block:
                self._pending_refresh_from_block = changed_block
            if self._refresh_timer is not None:
                self._refresh_timer.cancel()
            timer = threading.Timer(
                self._CLIENT_REFRESH_DELAY_SECONDS,
                self._flush_client_block_refreshes,
            )
            timer.daemon = True
            self._refresh_timer = timer
            timer.start()

    def _flush_client_block_refreshes(self) -> None:
        with self._refresh_lock:
            changed_block = self._pending_refresh_from_block
            self._pending_refresh_from_block = None
            self._refresh_timer = None
        if changed_block is None:
            return
        payloads: dict[int, dict[str, object]] = {}
        for playback in self._server.get_client_playbacks().values():
            for block_index in sorted(playback._loaded_blocks):
                if block_index < changed_block:
                    continue
                payload = payloads.get(block_index)
                if payload is None:
                    payload = self._timeline.block_payload(block_index)
                    payloads[block_index] = payload
                playback._send_runtime_call("loadBlock", payload)

    def _broadcast_scene_message(
        self, message: _messages.Message, start_step: int
    ) -> None:
        for client in self._server.get_clients().values():
            if message.excluded_self_client == client.client_id:
                continue
            playback = self._server.get_client_playback(client.client_id)
            if playback is None or playback.current_timestep < start_step:
                continue
            impl.queue_client_message(client, message)

    def _validate_step_messages(self, messages: list[_messages.Message]) -> None:
        created_names: set[str] = set()
        for message in messages:
            if not isinstance(message, _messages._CreateSceneNodeMessage):
                continue
            name = message.name
            if name in self._live_scene._handle_from_node_name:
                raise RuntimeError(
                    f"Cannot create timeline node {name!r} because a static scene node "
                    "with the same name already exists."
                )
            if self._timeline.has_node(name) or name in created_names:
                raise RuntimeError(
                    f"Cannot create timeline node {name!r} more than once. "
                    "Create it once and update the returned handle inside later "
                    "server.at(t) blocks."
                )
            created_names.add(name)


class _TimelineTransport(WebsockMessageHandler):
    def __init__(self, server: Viser4dServer, recorder: SceneRecorder) -> None:
        super().__init__()
        self._server = server
        self._recorder = recorder
        self._started = False

    def start(self) -> None:
        self._started = True

    def get_message_buffer(self) -> Any:
        return self

    def push(self, message: _messages.Message) -> None:
        if not self._started:
            return
        pending_messages = self._recorder._pending_messages
        if pending_messages is not None:
            pending_messages.append(message)
            return
        self._recorder._record_live_scene_message(message)

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
