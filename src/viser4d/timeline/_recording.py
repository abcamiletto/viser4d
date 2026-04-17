from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator

import numpy as np

from .. import _viser_private as impl
from .._types import RuntimeBlockPayload
from ..audio._api import AudioApi, AudioHandle, AudioState, audio_array_payload
from ..audio._messages import AddAudioMessage
from ._messages_util import (
    scene_delete_state_key,
    scene_entries_for_message,
    store_raw_message,
)
from ._playback import manifest_payload

if TYPE_CHECKING:
    from .._server import Viser4dServer
    from ._store import TimelineStore


@dataclass(frozen=True)
class TimelineContext:
    """Scene and audio APIs exposed inside ``server.at(t)``."""

    scene: impl.SceneApi
    audio: AudioApi


@dataclass
class _WriteSession:
    step: int
    messages: list[impl.Message] = field(default_factory=list)


class SceneRecorder:
    """Capture scene and audio edits into the timeline store."""

    _CLIENT_REFRESH_DELAY_SECONDS = 0.05

    def __init__(self, server: Viser4dServer) -> None:
        self._server = server
        self._live_scene = server.scene
        self._pending_refresh_from_block: int | None = None
        self._refresh_timer: threading.Timer | None = None
        self._refresh_lock = threading.Lock()
        self._timeline_lock = threading.RLock()
        self._active_session: _WriteSession | None = None
        self._transport = _TimelineTransport(server, self)
        owner = _TimelineSceneOwner(self._transport)
        self.scene = impl.create_scene_api(
            owner,
            thread_executor=impl.server_thread_executor(server),
            event_loop=server.get_event_loop(),
        )
        # Reuse viser's existing server-owned client lookup for inbound events.
        impl.set_scene_owner(self.scene, server)
        self.audio = AudioApi(self)
        self._transport.start()

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[TimelineContext]:
        """Record scene and audio mutations into timestep ``t``."""
        changed_block: int | None = None
        with self._timeline_lock:
            if self._active_session is not None:
                raise RuntimeError("server.at(t) cannot be nested.")
            session = _WriteSession(step=self._server._timeline.validate_step(t))
            self._active_session = session
            try:
                yield TimelineContext(scene=self.scene, audio=self.audio)
            finally:
                self._active_session = None
                if session.messages:
                    self._validate_step_messages(session.messages)
                    self._server._timeline.record_step(session.step, session.messages)
                    changed_block = self._server._timeline.block_index_for_step(
                        session.step
                    )
        if changed_block is not None:
            self._queue_client_block_refresh(changed_block)

    def add_audio(
        self,
        name: str,
        *,
        data: np.ndarray,
        sample_rate: int,
    ) -> AudioHandle:
        """Create a timeline-owned audio track inside the active write session."""
        session = self._active_session
        if session is None:
            raise RuntimeError(
                "timeline.audio.add_track() is only valid inside server.at(t)."
            )
        state = AudioState(
            name=name,
            sample_rate=sample_rate,
            waveform=np.ascontiguousarray(data),
        )
        session.messages.append(
            AddAudioMessage(
                name=name,
                sampleRate=state.sample_rate,
                waveform=audio_array_payload(state.waveform),
                volume=state.volume,
            )
        )
        handle = AudioHandle(self.dispatch_audio_update, state)
        return handle

    def route_message(self, message: impl.Message) -> None:
        """Route a timeline-scene message to the current recording session."""
        session = self._active_session
        if session is not None:
            session.messages.append(message)
            return
        stored_message = store_raw_message(message)
        with self._timeline_lock:
            if impl.is_create_scene_node_message(message):
                raise RuntimeError(
                    "Timeline scene node creation is only valid inside server.at(t)."
                )
            self._server._timeline.record_scene_override(stored_message)
            puts, delete_nodes = scene_entries_for_message(stored_message)
        # Forward scene overrides directly to connected clients. They live in a
        # client-side overlay, independent of recorded block state.
        for entry in puts:
            for playback in self._server.get_client_playbacks().values():
                playback.apply_message_update(entry["key"], entry["message"])
        for node_name in delete_nodes:
            delete_key = scene_delete_state_key(node_name)
            for playback in self._server.get_client_playbacks().values():
                playback.apply_message_update(delete_key, stored_message)

    def dispatch_audio_update(self, message: impl.Message) -> None:
        """Route audio handle updates to the active session or live runtimes."""
        session = self._active_session
        if session is not None:
            session.messages.append(message)
            return
        stored_message = store_raw_message(message)
        for playback in self._server.get_client_playbacks().values():
            playback.apply_message_update("audio", stored_message)

    def close(self) -> None:
        """Stop any deferred client refresh work."""
        self._cancel_pending_refresh()

    def resize_timeline(self, num_steps: int) -> TimelineStore:
        """Replace the timeline with a resized copy."""
        return self._replace_timeline(
            lambda timeline: timeline.resized_copy(num_steps),
            "server.set_steps() cannot run while inside server.at(t).",
        )

    def clear_timeline(self) -> TimelineStore:
        """Replace the timeline with an empty copy."""
        return self._replace_timeline(
            lambda timeline: timeline.empty_copy(),
            "server.clear() cannot run while inside server.at(t).",
        )

    def _cancel_pending_refresh(self) -> None:
        with self._refresh_lock:
            timer = self._refresh_timer
            self._refresh_timer = None
            self._pending_refresh_from_block = None
        if timer is not None:
            timer.cancel()

    def _replace_timeline(
        self,
        replace: Callable[[TimelineStore], TimelineStore],
        active_session_error: str,
    ) -> TimelineStore:
        self._cancel_pending_refresh()
        if self._active_session is not None:
            raise RuntimeError(active_session_error)
        with self._timeline_lock:
            old_timeline = self._server._timeline
            self._server.bump_client_chunk_cache_version()
            self._server._timeline = replace(old_timeline)
        return old_timeline

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
        self._server.bump_client_chunk_cache_version()
        payloads: dict[int, RuntimeBlockPayload] = {}
        with self._timeline_lock:
            manifests = [
                manifest_payload(manifest)
                for manifest in self._server._timeline.block_manifests()
            ]
            for playback in self._server.get_client_playbacks().values():
                for block_index in sorted(playback.loaded_blocks):
                    if block_index < changed_block:
                        continue
                    payload = payloads.get(block_index)
                    if payload is None:
                        payload = self._server._timeline.block_payload(block_index)
                        payloads[block_index] = payload
                    playback.update_block(payload)
                playback.send_manifests(manifests)

    def _validate_step_messages(self, messages: list[impl.Message]) -> None:
        for message in messages:
            if not impl.is_create_scene_node_message(message):
                continue
            name = message.name  # type: ignore[union-attr]
            if impl.scene_has_node(self._live_scene, name):
                raise RuntimeError(
                    f"Cannot create timeline node {name!r} because a static scene node "
                    "with the same name already exists."
                )


class _TimelineTransport(impl.WebsockMessageHandler):
    """Minimal transport that feeds ``SceneApi`` messages back into the recorder."""

    def __init__(self, server: Viser4dServer, recorder: SceneRecorder) -> None:
        super().__init__()
        self._server = server
        self._recorder = recorder
        self._started = False

    def start(self) -> None:
        self._started = True

    def get_message_buffer(self) -> Any:
        return self

    def push(self, message: impl.Message) -> None:
        if not self._started:
            return
        self._recorder.route_message(message)

    def atomic_start(self) -> None:
        pass

    def atomic_end(self) -> None:
        pass

    def register_handler(self, message_cls: type[Any], callback: Any) -> None:
        impl.register_message_handler(self._server, message_cls, callback)

    def unregister_handler(self, message_cls: type[Any], callback: Any = None) -> None:
        impl.unregister_message_handler(self._server, message_cls, callback)


class _TimelineSceneOwner:
    """Minimal client-like owner required by ``SceneApi``."""

    def __init__(self, transport: _TimelineTransport) -> None:
        self._websock_connection = transport
