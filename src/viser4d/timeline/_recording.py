from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Iterator, cast

import numpy as np
from viser import _messages
from viser._scene_api import SceneApi
from viser._scene_handles import (
    SceneNodePointerEvent,
    ScenePointerEvent,
    TransformControlsEvent,
    _ClickableSceneNodeHandle,
)
from viser._threadpool_exceptions import print_threadpool_errors
from viser.infra import ClientId, WebsockMessageHandler

from .. import _viser_private as impl
from ..audio._api import AudioHandle, AudioState, audio_array_payload
from ..audio._messages import AddAudioMessage
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
        self._transport = _TimelineTransport(self)
        owner = _TimelineSceneOwner(server, self._transport)
        # SceneApi only reads the client-like fields defined on _TimelineSceneOwner.
        self.scene = SceneApi(
            owner,  # type: ignore[arg-type]
            thread_executor=server._thread_executor,
            event_loop=server.get_event_loop(),
        )
        owner.scene = self.scene
        self._transport.start()
        server._websock_server.register_handler(
            _messages.SceneNodeClickMessage,
            self._handle_node_click,
        )
        server._websock_server.register_handler(
            _messages.TransformControlsUpdateMessage,
            self._handle_transform_controls_update,
        )
        server._websock_server.register_handler(
            _messages.TransformControlsDragStartMessage,
            self._handle_transform_controls_drag_start,
        )
        server._websock_server.register_handler(
            _messages.TransformControlsDragEndMessage,
            self._handle_transform_controls_drag_end,
        )
        server._websock_server.register_handler(
            _messages.ScenePointerMessage,
            self._handle_scene_pointer,
        )

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
        stored_message = store_raw_message(message)
        self._server._send_runtime_call(
            "applyMessageUpdate", serialize_stored_message(stored_message)
        )

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
            if self._push_live_message(message):
                return
            raise RuntimeError(
                "Timeline scene mutations are only valid inside server.at(t)."
            )
        self._validate_create_message(message)
        self._pending_messages.append(message)

    def _push_live_message(self, message: _messages.Message) -> bool:
        if isinstance(message, _messages.SetSceneNodeClickableMessage):
            self._server._send_runtime_call(
                "applyMessageUpdate",
                serialize_stored_message(store_raw_message(message)),
            )
            return True
        if isinstance(message, _messages.ScenePointerEnableMessage):
            impl.queue_server_message(self._server, message)
            return True
        if isinstance(
            message,
            _messages.SetPositionMessage | _messages.SetOrientationMessage,
        ):
            for client in self._server.get_clients().values():
                impl.queue_client_message(client, message)
            return True
        return False

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

    async def _handle_node_click(
        self,
        client_id: ClientId,
        message: _messages.SceneNodeClickMessage,
    ) -> None:
        handle = self.scene._handle_from_node_name.get(message.name)
        client = self._server.get_clients().get(int(client_id))
        if handle is None or client is None or not handle._impl.click_cb:
            return
        event = SceneNodePointerEvent(
            client=client,
            client_id=int(client_id),
            event="click",
            target=cast(_ClickableSceneNodeHandle, handle),
            ray_origin=message.ray_origin,
            ray_direction=message.ray_direction,
            screen_pos=message.screen_pos,
            instance_index=message.instance_index,
        )
        for callback in handle._impl.click_cb:
            await self._dispatch_callback(callback, event)

    async def _handle_transform_controls_update(
        self,
        client_id: ClientId,
        message: _messages.TransformControlsUpdateMessage,
    ) -> None:
        handle = self.scene._handle_from_transform_controls_name.get(message.name)
        if handle is None:
            return
        handle._impl.wxyz = np.array(message.wxyz)
        handle._impl.position = np.array(message.position)
        handle._impl_aux.last_updated = time.time()
        event = TransformControlsEvent(
            client=self._server.get_clients().get(int(client_id)),
            client_id=int(client_id),
            target=handle,
        )
        for callback in handle._impl_aux.update_cb:
            await self._dispatch_callback(callback, event)
        if handle._impl_aux.sync_cb is not None:
            handle._impl_aux.sync_cb(client_id, handle)

    async def _handle_transform_controls_drag_start(
        self,
        client_id: ClientId,
        message: _messages.TransformControlsDragStartMessage,
    ) -> None:
        handle = self.scene._handle_from_transform_controls_name.get(message.name)
        if handle is None:
            return
        event = TransformControlsEvent(
            client=self._server.get_clients().get(int(client_id)),
            client_id=int(client_id),
            target=handle,
        )
        for callback in handle._impl_aux.drag_start_cb:
            await self._dispatch_callback(callback, event)

    async def _handle_transform_controls_drag_end(
        self,
        client_id: ClientId,
        message: _messages.TransformControlsDragEndMessage,
    ) -> None:
        handle = self.scene._handle_from_transform_controls_name.get(message.name)
        if handle is None:
            return
        event = TransformControlsEvent(
            client=self._server.get_clients().get(int(client_id)),
            client_id=int(client_id),
            target=handle,
        )
        for callback in handle._impl_aux.drag_end_cb:
            await self._dispatch_callback(callback, event)

    async def _handle_scene_pointer(
        self,
        client_id: ClientId,
        message: _messages.ScenePointerMessage,
    ) -> None:
        callback = self.scene._scene_pointer_cb
        client = self._server.get_clients().get(int(client_id))
        if callback is None or client is None:
            return
        event = ScenePointerEvent(
            client=client,
            client_id=int(client_id),
            event_type=message.event_type,
            ray_origin=message.ray_origin,
            ray_direction=message.ray_direction,
            screen_pos=message.screen_pos,
        )
        await self._dispatch_callback(callback, event)

    async def _dispatch_callback(self, callback: Any, event: Any) -> None:
        if asyncio.iscoroutinefunction(callback):
            await callback(event)
            return
        self._server._thread_executor.submit(callback, event).add_done_callback(
            print_threadpool_errors
        )


class _TimelineTransport(WebsockMessageHandler):
    def __init__(self, recorder: SceneRecorder) -> None:
        super().__init__()
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


class _TimelineSceneOwner:
    """Minimal client-like owner required by ``SceneApi``."""

    scene: SceneApi

    def __init__(self, server: Viser4dServer, transport: _TimelineTransport) -> None:
        self._websock_connection = transport
        self._viser_server = server
        self.client_id = -1

    def flush(self) -> None:
        pass
