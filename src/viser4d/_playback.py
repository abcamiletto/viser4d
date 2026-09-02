"""Per-client protocol bridge and public playback handle.

One ``ClientSession`` exists per connected browser. It buffers outbound control
messages until the runtime signals ``TimelineReadyMessage``, serves block
payloads on request, and mirrors the client's transport state back to the
server-provided callbacks.

Threading: viser's event loop delivers inbound events and owns the outbound
queue; block payloads are built on viser's thread executor (folding a checkpoint
is slow) and sent from the event loop. One lock guards this session's state.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor

from . import _state, _viser
from ._config import PlaybackConfig
from ._protocol import (
    TimelineBlockBytesMessage,
    TimelineBlockDiscardMessage,
    TimelineBlockMessage,
    TimelineBlockRequestMessage,
    TimelineClearMessage,
    TimelineConfigureMessage,
    TimelineEventMessage,
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
from ._timeline import Timeline
from ._viser import ClientHandle


class ClientSession:
    """Playback controls and block serving for one connected client."""

    def __init__(
        self,
        client: ClientHandle,
        timeline: Timeline,
        config: PlaybackConfig,
        *,
        executor: ThreadPoolExecutor,
        event_loop: asyncio.AbstractEventLoop,
        on_timestep: Callable[[ClientHandle, int], None],
        on_playback: Callable[[ClientHandle, bool], None],
    ) -> None:
        self._client = client
        self._timeline = timeline
        self._config = config
        self._executor = executor
        self._event_loop = event_loop
        self._on_timestep = on_timestep
        self._on_playback = on_playback
        self._lock = threading.RLock()
        self._speed = config.speed
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
        self._send(TimelinePlayMessage(speed=speed, loop=self._config.loop))

    def pause(self) -> None:
        self._send(TimelinePauseMessage())

    def seek(self, t: int) -> None:
        t = self._timeline.validate_step(t)
        with self._lock:
            self._current_timestep = t
        self._send(TimelineSeekMessage(step=t))

    def refresh(self) -> None:
        self._send(TimelineRefreshMessage())

    def set_speed(self, speed: float) -> None:
        with self._lock:
            self._speed = speed
        self._send(TimelineSetSpeedMessage(speed=speed, loop=self._config.loop))

    # -- server-driven syncs ----------------------------------------------

    def sync_config(self) -> None:
        self._send_configure()

    def sync_steps(self) -> None:
        max_step = self._timeline.num_steps - 1
        with self._lock:
            self._current_timestep = min(self._current_timestep, max_step)
            target = self._current_timestep
        self._reset(target)

    def clear(self) -> None:
        with self._lock:
            self._speed = self._config.speed
            self._is_playing = False
            self._current_timestep = 0
            if not self._ready:
                self._pending_messages = []
        self._reset(0)

    def update_block(self, index: int, message: TimelineBlockMessage) -> None:
        with self._lock:
            if index in self._loaded_blocks:
                self._send(message)

    def send_block_bytes(self, block_bytes: list[int | None]) -> None:
        self._send(TimelineBlockBytesMessage(blockBytes=block_bytes))

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
            if message.speed <= 0.0:
                raise ValueError(f"Client sent invalid speed {message.speed!r}.")
            with self._lock:
                self._speed = message.speed
        elif isinstance(message, TimelinePlaybackStateMessage):
            self._handle_playback_state(message.isPlaying)

    def _handle_timestep(self, step: int) -> None:
        self._timeline.validate_step(step)
        with self._lock:
            self._current_timestep = step
        self._on_timestep(self._client, step)

    def _handle_playback_state(self, is_playing: bool) -> None:
        with self._lock:
            if is_playing == self._is_playing:
                return
            self._is_playing = is_playing
        self._on_playback(self._client, is_playing)

    def _handle_block_request(self, index: int) -> None:
        self._timeline.validate_block(index)
        with self._lock:
            if index in self._pending_requests:
                return
            self._pending_requests.add(index)
        future = self._executor.submit(self._timeline.block_message, index)
        future.add_done_callback(
            lambda f: self._event_loop.call_soon_threadsafe(
                self._finish_block_request, index, f
            )
        )

    def _finish_block_request(
        self, index: int, future: Future[TimelineBlockMessage]
    ) -> None:
        message = future.result()
        with self._lock:
            if index not in self._pending_requests:
                return
            self._pending_requests.discard(index)
            self._loaded_blocks.add(index)
            self._send(message)
            # block_message() filled in this block's encoded size; refresh the
            # client's sizes so its preload planner can use its budget.
            self.send_block_bytes(self._timeline.block_bytes())

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
                numSteps=self._timeline.num_steps,
                blockSize=self._config.streaming.block_size,
                timelineFps=self._config.fps,
                speed=speed,
                loop=self._config.loop,
                cacheBytes=self._config.streaming.client_cache_bytes,
                blockBytes=self._timeline.block_bytes(),
            )
        )

    def _sync_overrides(self) -> None:
        for entry in self._timeline.override_items():
            self.apply_override(entry)

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
