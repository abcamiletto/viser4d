from __future__ import annotations

import threading
import warnings
from concurrent.futures import Future
from typing import TYPE_CHECKING

import viser

from .. import _viser_private as impl
from .._hybrid import inflate_stored_message, inflate_stored_messages
from .._runtime_messages import (
    RuntimeApplyMessageUpdateMessage,
    RuntimeBlockRequestMessage,
    RuntimeClearMessage,
    RuntimeConfigureMessage,
    RuntimeEvictBlockMessage,
    RuntimeEventMessage,
    RuntimeLoadBlockMessage,
    RuntimePauseMessage,
    RuntimePatchBlockMessage,
    RuntimeBlockStepDelta,
    RuntimePlaybackStateMessage,
    RuntimePlayMessage,
    RuntimeReadyMessage,
    RuntimeRefreshMessage,
    RuntimeSeekMessage,
    RuntimeSetSpeedMessage,
    RuntimeSpeedMessage,
    RuntimeTimestepMessage,
    runtime_scene_message,
    runtime_scene_messages,
)
from .._types import (
    ClientRuntimeConfig,
    RuntimeBlockDeltaPayload,
    RuntimeBlockPayload,
    StoredMessage,
)
from .._validation import require_positive_float
from ._streaming import PreloadPlanner

if TYPE_CHECKING:
    from .._viser_private import ClientHandle
    from .._server import Viser4dServer


class ClientPlaybackHandle:
    """Per-client playback controls backed by the injected browser runtime.

    The visible playback widgets stay client-local in the browser. Python only
    mirrors runtime state changes and sends explicit server-side commands.
    """

    def __init__(
        self,
        server: Viser4dServer,
        client: ClientHandle,
        brand_color: tuple[int, int, int] | None = None,
    ) -> None:
        self._server = server
        self._client = client
        self._speed = server.playback_speed
        self._is_playing = False
        self._current_timestep = 0
        self._loaded_blocks: set[int] = set()
        self._pending_block_loads: dict[int, Future[RuntimeBlockPayload]] = {}
        self._pending_runtime_messages: list[impl.Message] = []
        self._runtime_ready = False
        self._lock = threading.RLock()
        self._create_gui(brand_color)
        self.sync_runtime_config()
        self._sync_loaded_blocks(self._current_timestep, force=True)
        # New clients need the initial timeline scene state before playback starts.
        self.seek(self._current_timestep)

    @property
    def loaded_blocks(self) -> set[int]:
        """Return a snapshot of the currently loaded block indices."""
        with self._lock:
            return set(self._loaded_blocks)

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def current_timestep(self) -> int:
        return self._current_timestep

    def play(self) -> None:
        """Start playback on this client."""
        with self._lock:
            next_speed = self._speed
        next_loop = self._server.loop
        self._send_runtime_message(RuntimePlayMessage(speed=next_speed, loop=next_loop))

    def pause(self) -> None:
        """Pause playback on this client."""
        self._send_runtime_message(RuntimePauseMessage())

    def seek(self, t: int) -> None:
        """Seek this client to timestep ``t``."""
        t = self._require_timestep(t)
        with self._lock:
            self._current_timestep = t
        self._sync_loaded_blocks(t)
        self._timeline_slider.value = t
        self._send_runtime_message(RuntimeSeekMessage(step=t))

    def refresh(self) -> None:
        """Redraw this client's current timestep from recorded timeline state."""
        self._sync_loaded_blocks(self.current_timestep)
        self._send_runtime_message(RuntimeRefreshMessage())

    def set_speed(self, speed: float) -> None:
        """Update playback speed on this client relative to timeline cadence."""
        next_speed = require_positive_float("speed", speed)
        with self._lock:
            self._speed = next_speed
        next_loop = self._server.loop
        self._speed_slider.value = next_speed
        self._send_runtime_message(
            RuntimeSetSpeedMessage(speed=next_speed, loop=next_loop)
        )

    def sync_steps(self) -> None:
        """Resync this client after the server timeline length changes."""
        max_step = self._server.num_steps - 1
        with self._lock:
            current_timestep = min(self._current_timestep, max_step)
            previous_blocks = set(self._loaded_blocks)
            stale_futures = list(self._pending_block_loads.values())
            self._loaded_blocks = set()
            self._pending_block_loads = {}
        for future in stale_futures:
            future.result()
        for block_index in sorted(previous_blocks):
            self._send_runtime_message(RuntimeEvictBlockMessage(block=block_index))
        self._timeline_slider.max = max_step
        self.sync_runtime_config()
        self.seek(current_timestep)

    def clear(self) -> None:
        """Reset client playback state and clear the browser runtime."""
        with self._lock:
            stale_futures = list(self._pending_block_loads.values())
            self._speed = 1.0
            self._is_playing = False
            self._loaded_blocks = set()
            self._pending_block_loads = {}
            if not self._runtime_ready:
                self._pending_runtime_messages = []
        for future in stale_futures:
            future.result()
        self._speed_slider.value = self._speed
        self._send_runtime_message(RuntimeClearMessage())
        self.sync_runtime_config()
        self.seek(0)

    def apply_message_update(self, message: StoredMessage) -> None:
        """Forward one live stored message into the browser runtime."""
        self._send_runtime_message(
            RuntimeApplyMessageUpdateMessage(
                message=runtime_scene_message(inflate_stored_message(message))
            )
        )

    def load_block(self, payload: RuntimeBlockPayload) -> None:
        """Inflate and send one timeline block payload to the browser runtime."""
        self._send_runtime_message(
            RuntimeLoadBlockMessage(
                block=payload["block"],
                checkpointMessages=runtime_scene_messages(
                    inflate_stored_messages(payload["checkpointMessages"])
                ),
                stepMessages=[
                    runtime_scene_messages(inflate_stored_messages(step_messages))
                    for step_messages in payload["stepMessages"]
                ],
            )
        )

    def patch_block(self, payload: RuntimeBlockDeltaPayload) -> None:
        """Patch one already loaded timeline block in the browser runtime."""
        checkpoint_messages = payload["checkpointMessages"]
        self._send_runtime_message(
            RuntimePatchBlockMessage(
                block=payload["block"],
                checkpointMessages=(
                    None
                    if checkpoint_messages is None
                    else runtime_scene_messages(
                        inflate_stored_messages(checkpoint_messages)
                    )
                ),
                stepDeltas=[
                    RuntimeBlockStepDelta(
                        offset=step_delta["offset"],
                        messages=runtime_scene_messages(
                            inflate_stored_messages(step_delta["messages"])
                        ),
                    )
                    for step_delta in payload["stepDeltas"]
                ],
            )
        )

    def handle_runtime_event(self, message: RuntimeEventMessage) -> None:
        """Mirror browser runtime events back into the Python playback state."""
        if isinstance(message, RuntimeReadyMessage):
            with self._lock:
                if self._runtime_ready:
                    return
                self._runtime_ready = True
                pending_messages = self._pending_runtime_messages
                self._pending_runtime_messages = []
            for pending_message in pending_messages:
                impl.queue_client_message(self._client, pending_message)
            return
        if isinstance(message, RuntimeBlockRequestMessage):
            if self._ignore_invalid_runtime_step(message.step):
                return
            self._sync_loaded_blocks(message.step, force=True)
            return
        if isinstance(message, RuntimeTimestepMessage):
            if self._ignore_invalid_runtime_step(message.step):
                return
            timestep = message.step
            with self._lock:
                self._current_timestep = timestep
            self._sync_loaded_blocks(timestep)
            self._server._dispatch_timestep_change(self._client, timestep)
            return
        if isinstance(message, RuntimeSpeedMessage):
            with self._lock:
                self._speed = require_positive_float("speed", message.speed)
            return
        if isinstance(message, RuntimePlaybackStateMessage):
            with self._lock:
                if message.isPlaying == self._is_playing:
                    return
                self._is_playing = message.isPlaying
            self._server._dispatch_playback_change(self._client, message.isPlaying)

    def sync_runtime_config(self) -> None:
        """Send the current playback config and GUI ids to the browser runtime."""
        with self._lock:
            speed = self._speed
        loop = self._server.loop
        self._send_runtime_message(
            RuntimeConfigureMessage(
                **ClientRuntimeConfig(
                    numSteps=self._server.num_steps,
                    blockSize=self._server.block_size,
                    timelineFps=self._server.fps,
                    speed=speed,
                    loop=loop,
                    timelineSliderUuid=impl.gui_uuid(self._timeline_slider),
                    speedSliderUuid=impl.gui_uuid(self._speed_slider),
                    stepButtonsUuid=impl.gui_uuid(self._step_buttons),
                    playButtonUuid=impl.gui_uuid(self._play_button),
                    pauseButtonUuid=impl.gui_uuid(self._pause_button),
                )
            ),
        )

    def _create_gui(self, brand_color: tuple[int, int, int] | None) -> None:
        """Create the per-client playback controls."""
        max_step = self._server.num_steps - 1
        gui = self._client.gui
        # High order so the playback folder always sorts below user-added GUI.
        with gui.add_folder("Playback", order=_PLAYBACK_ORDER):
            self._timeline_slider = gui.add_slider(
                "Timestep", min=0, max=max_step, step=1, initial_value=0
            )
            self._speed_slider = gui.add_slider(
                "Speed", min=0.1, max=4.0, step=0.1, initial_value=self._speed
            )
            self._step_buttons = gui.add_button_group("Step", ("Prev", "Next"))
            self._play_button = gui.add_button(
                "Play", icon=viser.Icon.PLAYER_PLAY_FILLED
            )
            self._pause_button = gui.add_button(
                "Pause",
                color=_pause_button_color(brand_color),
                icon=viser.Icon.PLAYER_PAUSE_FILLED,
                visible=False,
            )

    def _require_timestep(self, timestep: int) -> int:
        if 0 <= timestep < self._server.num_steps:
            return timestep
        raise ValueError(
            f"timestep must be in [0, {self._server.num_steps - 1}], got {timestep}."
        )

    def _ignore_invalid_runtime_step(self, step: int) -> bool:
        if 0 <= step < self._server.num_steps:
            return False
        warnings.warn(
            f"Ignoring runtime event with invalid step={step}.",
            RuntimeWarning,
            stacklevel=3,
        )
        return True

    def _sync_loaded_blocks(self, timestep: int, *, force: bool = False) -> None:
        """Load the current block first, then previous, then budgeted forward blocks."""
        timeline = self._server._timeline
        current_block = timeline.block_index_for_step(timestep)
        manifests = timeline.block_manifests()
        with self._lock:
            previous = set(self._loaded_blocks)
            pending = set(self._pending_block_loads)
        plan = PreloadPlanner.plan(
            current_block,
            manifests,
            self._server.client_chunk_cache_bytes,
            loaded_blocks=previous,
            pending_blocks=pending,
            force=force,
        )
        with self._lock:
            self._loaded_blocks = set(plan.desired_blocks)
        for block_index in plan.required_loads:
            self._queue_block_load(block_index)
        for block_index in plan.speculative_loads:
            self._queue_block_load(block_index)
        for block_index in plan.evictions:
            self._send_runtime_message(RuntimeEvictBlockMessage(block=block_index))

    def _queue_block_load(self, block_index: int) -> None:
        with self._lock:
            if block_index in self._pending_block_loads:
                return
            future = impl.server_thread_executor(self._server).submit(
                self._server._timeline.block_payload, block_index
            )
            self._pending_block_loads[block_index] = future
        future.add_done_callback(
            lambda f: self._server.get_event_loop().call_soon_threadsafe(
                self._finish_block_load, block_index, f
            )
        )

    def _finish_block_load(
        self,
        block_index: int,
        future: Future[RuntimeBlockPayload],
    ) -> None:
        with self._lock:
            if self._pending_block_loads.get(block_index) is not future:
                return
            self._pending_block_loads.pop(block_index)
        payload = future.result()
        with self._lock:
            should_send = block_index in self._loaded_blocks
            current_timestep = self._current_timestep
        if should_send:
            self.load_block(payload)
        self._sync_loaded_blocks(current_timestep)

    def _send_runtime_message(self, message: impl.Message) -> None:
        with self._lock:
            if not self._runtime_ready:
                self._pending_runtime_messages.append(message)
                return
        impl.queue_client_message(self._client, message)


_PLAYBACK_ORDER = -1e9


def _pause_button_color(
    brand_color: tuple[int, int, int] | None,
) -> tuple[int, int, int]:
    r, g, b = (34, 139, 230) if brand_color is None else brand_color
    return (int(r * 0.85), int(g * 0.85), int(b * 0.85))
