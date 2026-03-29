from __future__ import annotations

import threading
from concurrent.futures import Future
from typing import TYPE_CHECKING, Any

import viser

from .. import _viser_private as impl
from .._types import RuntimeMethod, RuntimePayload
from .._runtime import client_runtime_config_payload, make_runtime_message
from .._validation import require_positive_float

if TYPE_CHECKING:
    from .._viser_private import ClientHandle
    from .._server import Viser4dServer


class ClientPlaybackHandle:
    """Per-client playback controls backed by the injected browser runtime.

    The visible playback widgets stay client-local in the browser. Python only
    observes committed timestep changes through the hidden sync control and
    sends explicit server-side broadcast commands.
    """

    def __init__(
        self,
        server: Viser4dServer,
        client: ClientHandle,
        brand_color: tuple[int, int, int] | None = None,
    ) -> None:
        self._server = server
        self._client = client
        self._speed = 1.0
        self._loop = False
        self._is_playing = False
        self._current_timestep = 0
        self._loaded_blocks: set[int] = set()
        self._pending_block_loads: set[int] = set()
        self._lock = threading.RLock()
        self._create_gui(brand_color)

        @self._block_request_sync.on_update
        def _request_blocks(_event: Any) -> None:
            step = self._require_timestep(int(self._block_request_sync.value))
            self._sync_loaded_blocks(step, force=True)

        @self._timestep_sync.on_update
        def _sync_timestep(_event: Any) -> None:
            timestep = self._require_timestep(int(self._timestep_sync.value))
            with self._lock:
                self._current_timestep = timestep
            self._sync_loaded_blocks(timestep)
            self._server._dispatch_timestep_change(self._client, timestep)

        @self._speed_sync.on_update
        def _sync_speed(_event: Any) -> None:
            with self._lock:
                self._speed = require_positive_float(
                    "speed", float(self._speed_sync.value)
                )

        @self._playback_state_sync.on_update
        def _sync_playback(_event: Any) -> None:
            is_playing = bool(self._playback_state_sync.value)
            with self._lock:
                if is_playing == self._is_playing:
                    return
                self._is_playing = is_playing
            self._server._dispatch_playback_change(self._client, is_playing)

        self._sync_runtime_config()
        self._sync_loaded_blocks(self._current_timestep, force=True)
        # New clients need the initial timeline scene state before playback starts.
        self.seek(self._current_timestep)

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def current_timestep(self) -> int:
        return self._current_timestep

    def play(self, speed: float | None = None, loop: bool | None = None) -> None:
        """Start playback on this client."""
        with self._lock:
            if speed is not None:
                self._speed = require_positive_float("speed", speed)
            if loop is not None:
                self._loop = bool(loop)
            payload = {"speed": self._speed, "loop": self._loop}
        self._speed_slider.value = payload["speed"]
        self._send_runtime_call("play", payload)

    def pause(self) -> None:
        """Pause playback on this client."""
        self._send_runtime_call("pause", {})

    def seek(self, t: int) -> None:
        """Seek this client to timestep ``t``."""
        t = self._require_timestep(t)
        with self._lock:
            self._current_timestep = t
        self._sync_loaded_blocks(t)
        self._timeline_slider.value = t
        self._send_runtime_call("seek", {"step": t})

    def refresh(self) -> None:
        """Redraw this client's current timestep from recorded timeline state."""
        self._sync_loaded_blocks(self.current_timestep)
        self._send_runtime_call("refresh", {})

    def set_speed(self, speed: float) -> None:
        """Update playback speed on this client relative to timeline cadence."""
        with self._lock:
            self._speed = require_positive_float("speed", speed)
            payload = {"speed": self._speed, "loop": self._loop}
        self._speed_slider.value = payload["speed"]
        self._send_runtime_call("setSpeed", payload)

    def _sync_runtime_config(self) -> None:
        with self._lock:
            speed, loop = self._speed, self._loop
        self._send_runtime_call(
            "configure",
            client_runtime_config_payload(
                num_steps=self._server.num_steps,
                block_size=self._server._timeline.block_size,
                timeline_fps=self._server.fps,
                speed=speed,
                loop=loop,
                block_request_sync_uuid=impl.gui_uuid(self._block_request_sync),
                timeline_slider_uuid=impl.gui_uuid(self._timeline_slider),
                speed_slider_uuid=impl.gui_uuid(self._speed_slider),
                step_buttons_uuid=impl.gui_uuid(self._step_buttons),
                play_button_uuid=impl.gui_uuid(self._play_button),
                pause_button_uuid=impl.gui_uuid(self._pause_button),
                speed_sync_uuid=impl.gui_uuid(self._speed_sync),
                playback_state_sync_uuid=impl.gui_uuid(self._playback_state_sync),
                timestep_sync_uuid=impl.gui_uuid(self._timestep_sync),
            ),
        )

    def _create_gui(self, brand_color: tuple[int, int, int] | None) -> None:
        max_step = self._server.num_steps - 1
        gui = self._client.gui
        self._block_request_sync = gui.add_number(
            "__viser4d_block_request_sync__",
            0,
            min=0,
            max=max_step,
            step=1,
            visible=False,
        )
        self._speed_sync = gui.add_number(
            "__viser4d_speed_sync__", self._speed, visible=False
        )
        self._playback_state_sync = gui.add_checkbox(
            "__viser4d_playback_state_sync__", False, visible=False
        )
        # Hidden control used by the browser runtime to report the active timestep back.
        self._timestep_sync = gui.add_number(
            "__viser4d_timestep_sync__", 0, min=0, max=max_step, step=1, visible=False
        )
        with gui.add_folder("Playback"):
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

    def _sync_loaded_blocks(self, timestep: int, *, force: bool = False) -> None:
        timeline = self._server._timeline
        current_block = timeline.block_index_for_step(timestep)
        desired = {current_block}
        if current_block + 1 < timeline.block_count:
            desired.add(current_block + 1)
        with self._lock:
            previous = set(self._loaded_blocks)
            self._loaded_blocks = desired
        for block_index in sorted(desired):
            if force or block_index not in previous:
                self._queue_block_load(block_index)
        for block_index in sorted(previous - desired):
            self._send_runtime_call("evictBlock", {"block": block_index})

    def _queue_block_load(self, block_index: int) -> None:
        with self._lock:
            if block_index in self._pending_block_loads:
                return
            self._pending_block_loads.add(block_index)
        future = impl.server_thread_executor(self._server).submit(
            self._server._timeline.block_payload, block_index
        )
        future.add_done_callback(
            lambda f: self._server.get_event_loop().call_soon_threadsafe(
                self._finish_block_load, block_index, f
            )
        )

    def _finish_block_load(
        self, block_index: int, future: Future[RuntimePayload]
    ) -> None:
        with self._lock:
            self._pending_block_loads.discard(block_index)
            should_send = block_index in self._loaded_blocks
        payload = future.result()
        if should_send:
            self._send_runtime_call("loadBlock", payload)

    def _send_runtime_call(
        self, method: RuntimeMethod, payload: RuntimePayload
    ) -> None:
        impl.queue_client_message(self._client, make_runtime_message(method, payload))


def _pause_button_color(
    brand_color: tuple[int, int, int] | None,
) -> tuple[int, int, int]:
    r, g, b = (34, 139, 230) if brand_color is None else brand_color
    return (int(r * 0.85), int(g * 0.85), int(b * 0.85))
