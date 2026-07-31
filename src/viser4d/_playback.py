"""Per-client protocol bridge and public playback handle.

One ``ClientSession`` exists per connected browser. It buffers outbound control
messages until the runtime signals ``TimelineReadyMessage``, serves block
payloads on request, and mirrors the client's transport state back to server
callbacks.
"""

from __future__ import annotations

import threading
import warnings
from concurrent.futures import Future
from typing import TYPE_CHECKING

from . import _state, _viser
from ._config import require_positive_float
from ._protocol import (
    BlockManifest,
    TimelineBlockDiscardMessage,
    TimelineBlockMessage,
    TimelineBlockRequestMessage,
    TimelineClearMessage,
    TimelineConfigureMessage,
    TimelineEventMessage,
    TimelineManifestsMessage,
    TimelinePauseMessage,
    TimelinePlaybackStateMessage,
    TimelinePlayMessage,
    TimelineReadyMessage,
    TimelineRefreshMessage,
    TimelineSeekMessage,
    TimelineSetSpeedMessage,
    TimelineSpeedMessage,
    TimelineTimestepMessage,
)

if TYPE_CHECKING:
    from ._server import Viser4dServer
    from ._viser import ClientHandle


class ClientSession:
    """Playback controls and block serving for one connected client."""

    def __init__(self, server: Viser4dServer, client: ClientHandle) -> None:
        self._server = server
        self._client = client
        self._lock = threading.RLock()
        self._speed = server.playback_speed
        self._is_playing = False
        self._current_timestep = 0
        self._loaded_blocks: set[int] = set()
        self._pending_requests: set[int] = set()
        self._pending_messages: list[_viser.Message] = []
        self._ready = False

    def start(self) -> None:
        """Send the initial sync. Called after the session is registered, so
        overrides recorded concurrently reach this client either way."""
        self._send_configure()
        self._send(TimelineSeekMessage(step=0))
        self._sync_overrides()

    # -- public playback handle -------------------------------------------

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def current_timestep(self) -> int:
        return self._current_timestep

    @property
    def loaded_blocks(self) -> set[int]:
        with self._lock:
            return set(self._loaded_blocks)

    def play(self) -> None:
        with self._lock:
            speed = self._speed
        self._send(TimelinePlayMessage(speed=speed, loop=self._server.loop))

    def pause(self) -> None:
        self._send(TimelinePauseMessage())

    def seek(self, t: int) -> None:
        t = self._require_timestep(t)
        with self._lock:
            self._current_timestep = t
        self._send(TimelineSeekMessage(step=t))

    def refresh(self) -> None:
        self._send(TimelineRefreshMessage())

    def set_speed(self, speed: float) -> None:
        next_speed = require_positive_float("speed", speed)
        with self._lock:
            self._speed = next_speed
        self._send(TimelineSetSpeedMessage(speed=next_speed, loop=self._server.loop))

    # -- server-driven syncs ----------------------------------------------

    def sync_config(self) -> None:
        self._send_configure()

    def sync_steps(self) -> None:
        max_step = self._server.num_steps - 1
        with self._lock:
            self._current_timestep = min(self._current_timestep, max_step)
            target = self._current_timestep
        self._reset(target)

    def clear(self) -> None:
        with self._lock:
            self._speed = self._server.playback_speed
            self._is_playing = False
            self._current_timestep = 0
            if not self._ready:
                self._pending_messages = []
        self._reset(0)

    def update_block(self, index: int, message: TimelineBlockMessage) -> None:
        with self._lock:
            if index in self._loaded_blocks:
                self._send(message)

    def send_manifests(self, manifests: list[BlockManifest]) -> None:
        self._send(TimelineManifestsMessage(manifests=manifests))

    def apply_override(self, entry: _state.SceneEntryRecord) -> None:
        self._send(_state.override_message(entry))

    # -- inbound events ---------------------------------------------------

    def handle_event(self, message: TimelineEventMessage) -> None:
        if isinstance(message, TimelineReadyMessage):
            self._flush_pending()
        elif isinstance(message, TimelineBlockRequestMessage):
            self._handle_block_request(message.index)
        elif isinstance(message, TimelineBlockDiscardMessage):
            with self._lock:
                self._loaded_blocks.discard(message.index)
                self._pending_requests.discard(message.index)
        elif isinstance(message, TimelineTimestepMessage):
            self._handle_timestep(message.step)
        elif isinstance(message, TimelineSpeedMessage):
            with self._lock:
                self._speed = require_positive_float("speed", message.speed)
        elif isinstance(message, TimelinePlaybackStateMessage):
            self._handle_playback_state(message.isPlaying)

    def _handle_timestep(self, step: int) -> None:
        if not 0 <= step < self._server.num_steps:
            warnings.warn(
                f"Ignoring timeline event with invalid step={step}.",
                RuntimeWarning,
                stacklevel=3,
            )
            return
        with self._lock:
            self._current_timestep = step
        self._server._dispatch_timestep_change(self._client, step)

    def _handle_playback_state(self, is_playing: bool) -> None:
        with self._lock:
            if is_playing == self._is_playing:
                return
            self._is_playing = is_playing
        self._server._dispatch_playback_change(self._client, is_playing)

    def _handle_block_request(self, index: int) -> None:
        if not 0 <= index < self._server._timeline.block_count:
            warnings.warn(
                f"Ignoring timeline block request with invalid index={index}.",
                RuntimeWarning,
                stacklevel=3,
            )
            return
        with self._lock:
            if index in self._pending_requests:
                return
            self._pending_requests.add(index)
        future = _viser.server_thread_executor(self._server).submit(
            self._server._timeline.block_message, index
        )
        future.add_done_callback(
            lambda f: self._server.get_event_loop().call_soon_threadsafe(
                self._finish_block_request, index, f
            )
        )

    def _finish_block_request(
        self, index: int, future: Future[TimelineBlockMessage]
    ) -> None:
        if future.exception() is not None:
            with self._lock:
                self._pending_requests.discard(index)
            return
        with self._lock:
            if index not in self._pending_requests:
                return
            self._pending_requests.discard(index)
            self._loaded_blocks.add(index)
            self._send(future.result())
            # block_message() filled in this block's byteSize; refresh the
            # client's manifests so its preload planner can use its budget.
            self.send_manifests(self._server._timeline.block_manifests())

    # -- reset / send primitives -----------------------------------------

    def _reset(self, target: int) -> None:
        with self._lock:
            self._loaded_blocks.clear()
            self._pending_requests.clear()
            self._send(TimelineClearMessage())
            self._send_configure()
            self._send(TimelineSeekMessage(step=target))
            self._sync_overrides()

    def _send_configure(self) -> None:
        with self._lock:
            speed = self._speed
        self._send(
            TimelineConfigureMessage(
                numSteps=self._server.num_steps,
                blockSize=self._server.block_size,
                timelineFps=self._server.fps,
                speed=speed,
                loop=self._server.loop,
                cacheBytes=self._server.streaming.client_cache_bytes,
                manifests=self._server._timeline.block_manifests(),
            )
        )

    def _sync_overrides(self) -> None:
        for entry in self._server._timeline.override_items():
            self.apply_override(entry)

    def _require_timestep(self, t: int) -> int:
        if 0 <= t < self._server.num_steps:
            return t
        raise ValueError(
            f"timestep must be in [0, {self._server.num_steps - 1}], got {t}."
        )

    def _flush_pending(self) -> None:
        # Drain under the lock so a concurrent _send cannot jump the queue.
        with self._lock:
            if self._ready:
                return
            self._ready = True
            for message in self._pending_messages:
                _viser.queue_client_message(self._client, message)
            self._pending_messages = []

    def _send(self, message: _viser.Message) -> None:
        with self._lock:
            if not self._ready:
                self._pending_messages.append(message)
                return
            _viser.queue_client_message(self._client, message)
