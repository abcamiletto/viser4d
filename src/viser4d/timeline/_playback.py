from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Any

import viser

from .. import _viser_private as impl
from .._types import RuntimeMethod, RuntimePayload
from .._runtime import (
    client_runtime_config_payload,
    make_runtime_message,
)
from .._validation import require_positive_float

_DEFAULT_PRIMARY_COLOR = (34, 139, 230)
_DEFAULT_SPEED = 1.0

if TYPE_CHECKING:
    from viser._viser import ClientHandle

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
        self._speed = _DEFAULT_SPEED
        self._loop = False
        self._is_playing = False
        self._current_timestep = 0
        self._lock = threading.RLock()
        max_step = max(server.num_steps - 1, 0)

        self._speed_sync = client.gui.add_number(
            "__viser4d_speed_sync__",
            self._speed,
            visible=False,
        )
        self._playback_state_sync = client.gui.add_checkbox(
            "__viser4d_playback_state_sync__",
            self._is_playing,
            visible=False,
        )
        # Hidden control used by the browser runtime to report the active timestep back.
        self._timestep_sync = client.gui.add_number(
            "__viser4d_timestep_sync__",
            self._current_timestep,
            min=0,
            max=max_step,
            step=1,
            visible=False,
        )
        with client.gui.add_folder("Playback"):
            self._timeline_slider = client.gui.add_slider(
                "Timestep",
                min=0,
                max=max_step,
                step=1,
                initial_value=self._current_timestep,
            )
            self._speed_slider = client.gui.add_slider(
                "Speed",
                min=0.1,
                max=4.0,
                step=0.1,
                initial_value=self._speed,
            )
            self._step_buttons = client.gui.add_button_group("Step", ("Prev", "Next"))
            self._play_button = client.gui.add_button(
                "Play",
                icon=viser.Icon.PLAYER_PLAY_FILLED,
            )
            self._pause_button = client.gui.add_button(
                "Pause",
                color=_pause_button_color(brand_color),
                icon=viser.Icon.PLAYER_PAUSE_FILLED,
                visible=False,
            )

        @self._timestep_sync.on_update
        def _sync(_event: Any) -> None:
            self._sync_from_client(int(self._timestep_sync.value))

        @self._speed_sync.on_update
        def _sync_speed(_event: Any) -> None:
            self._sync_speed_from_client(float(self._speed_sync.value))

        @self._playback_state_sync.on_update
        def _sync_playback(_event: Any) -> None:
            self._sync_playback_from_client(bool(self._playback_state_sync.value))

        self._sync_runtime_config()
        self._server._recorder.sync_client_timeline(client)
        # New clients need the initial timeline scene state before playback starts.
        self.seek(self._current_timestep)

    @property
    def speed(self) -> float:
        with self._lock:
            return self._speed

    @property
    def loop(self) -> bool:
        with self._lock:
            return self._loop

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return self._is_playing

    @property
    def current_timestep(self) -> int:
        with self._lock:
            return self._current_timestep

    def play(self, speed: float | None = None, loop: bool = False) -> None:
        """Start playback on this client."""
        with self._lock:
            if speed is not None:
                self._speed = require_positive_float("speed", speed)
            self._loop = bool(loop)
            payload = {"speed": self._speed, "loop": self._loop}
        self._set_speed_slider_value(payload["speed"])
        self._send_runtime_call("play", payload)

    def pause(self) -> None:
        """Pause playback on this client."""
        self._send_runtime_call("pause", {})

    def seek(self, t: int) -> None:
        """Seek this client to timestep ``t``."""
        if not 0 <= t < self._server.num_steps:
            raise ValueError(
                f"t must be in [0, {self._server.num_steps - 1}], got {t}."
            )
        with self._lock:
            self._current_timestep = t
        self._timeline_slider.value = t
        self._send_runtime_call("seek", {"step": t})

    def refresh(self) -> None:
        """Redraw this client's current timestep from recorded timeline state."""
        self._send_runtime_call("refresh", {})

    def set_speed(self, speed: float) -> None:
        """Update playback speed on this client relative to timeline cadence."""
        with self._lock:
            self._speed = require_positive_float("speed", speed)
            payload = {"speed": self._speed, "loop": self._loop}
        self._set_speed_slider_value(payload["speed"])
        self._send_runtime_call("setSpeed", payload)

    def _sync_runtime_config(self) -> None:
        """Send the current runtime configuration to the browser."""
        with self._lock:
            speed = self._speed
            loop = self._loop
        self._send_runtime_call(
            "configure",
            client_runtime_config_payload(
                num_steps=self._server.num_steps,
                timeline_fps=self._server.fps,
                speed=speed,
                loop=loop,
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
        self._timeline_slider.max = max(self._server.num_steps - 1, 0)
        self._timestep_sync.max = max(self._server.num_steps - 1, 0)

    def _sync_from_client(self, timestep: int) -> None:
        if not 0 <= timestep < self._server.num_steps:
            raise ValueError(
                f"timestep must be in [0, {self._server.num_steps - 1}], got {timestep}."
            )
        with self._lock:
            self._current_timestep = timestep
        self._server._dispatch_timestep_change(self._client, timestep)

    def _sync_speed_from_client(self, speed: float) -> None:
        with self._lock:
            self._speed = require_positive_float("speed", speed)

    def _sync_playback_from_client(self, is_playing: bool) -> None:
        with self._lock:
            if is_playing == self._is_playing:
                return
            self._is_playing = is_playing
        self._server._dispatch_playback_change(self._client, is_playing)

    def _send_runtime_call(
        self, method: RuntimeMethod, payload: RuntimePayload
    ) -> None:
        message = make_runtime_message(method, payload)
        impl.queue_client_message(self._client, message)

    def _set_speed_slider_value(self, speed: float) -> None:
        if self._speed_slider.value == speed:
            return
        self._speed_slider.value = speed


def _pause_button_color(
    brand_color: tuple[int, int, int] | None,
) -> tuple[int, int, int]:
    base_color = _DEFAULT_PRIMARY_COLOR if brand_color is None else brand_color
    return (
        int(base_color[0] * 0.85),
        int(base_color[1] * 0.85),
        int(base_color[2] * 0.85),
    )
