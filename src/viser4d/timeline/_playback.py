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

_DEFAULT_PRIMARY_COLOR = (34, 139, 230)

if TYPE_CHECKING:
    from viser._viser import ClientHandle

    from .._server import Viser4dServer


class ClientPlaybackHandle:
    """Per-client playback controls backed by the injected browser runtime.

    The visible playback widgets stay client-local in the browser. Python only
    observes committed timestep changes through the hidden sync control and
    sends explicit server-side broadcast commands.
    """

    def __init__(self, server: Viser4dServer, client: ClientHandle) -> None:
        self._server = server
        self._client = client
        self._fps = server.fps
        self._loop = False
        self._is_playing = False
        self._current_timestep = 0
        self._lock = threading.RLock()

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
            max=max(server.num_steps - 1, 0),
            step=1,
            visible=False,
        )
        with client.gui.add_folder("Playback"):
            self._timeline_slider = client.gui.add_slider(
                "Timestep",
                min=0,
                max=max(server.num_steps - 1, 0),
                step=1,
                initial_value=self._current_timestep,
            )
            self._fps_slider = client.gui.add_slider(
                "FPS",
                min=1.0,
                max=120.0,
                step=1.0,
                initial_value=self._fps,
            )
            self._step_buttons = client.gui.add_button_group("Step", ("Prev", "Next"))
            self._play_button = client.gui.add_button(
                "Play",
                icon=viser.Icon.PLAYER_PLAY_FILLED,
            )
            self._pause_button = client.gui.add_button(
                "Pause",
                color=_pause_button_color(None),
                icon=viser.Icon.PLAYER_PAUSE_FILLED,
                visible=False,
            )

        @self._timestep_sync.on_update
        def _sync(_event: Any) -> None:
            self._sync_from_client(int(self._timestep_sync.value))

        @self._playback_state_sync.on_update
        def _sync_playback(_event: Any) -> None:
            self._sync_playback_from_client(bool(self._playback_state_sync.value))

        self.sync_runtime_config()
        # New clients need the initial timeline scene state before playback starts.
        self.seek(self._current_timestep)

    @property
    def fps(self) -> float:
        with self._lock:
            return self._fps

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

    def play(self, fps: float | None = None, loop: bool = False) -> None:
        """Start playback on this client."""
        with self._lock:
            if fps is not None:
                self._fps = float(fps)
            self._loop = bool(loop)
            payload = {"fps": self._fps, "loop": self._loop}
        self._set_fps_slider_value(payload["fps"])
        self._send_runtime_call("play", payload)

    def pause(self) -> None:
        """Pause playback on this client."""
        self._send_runtime_call("pause", {})

    def seek(self, t: int) -> None:
        """Seek this client to timestep ``t``."""
        assert 0 <= t < self._server.num_steps
        self._set_current_timestep(t)
        self._send_runtime_call("seek", {"step": t})

    def refresh(self) -> None:
        """Redraw this client's current timestep from recorded timeline state."""
        self._send_runtime_call("refresh", {})

    def set_fps(self, fps: float) -> None:
        """Update playback speed on this client."""
        with self._lock:
            self._fps = float(fps)
            payload = {"fps": self._fps, "loop": self._loop}
        self._set_fps_slider_value(payload["fps"])
        self._send_runtime_call("setFps", payload)

    def sync_runtime_config(self) -> None:
        """Send the current runtime configuration to the browser."""
        with self._lock:
            fps = self._fps
            loop = self._loop
        self._send_runtime_call(
            "configure",
            client_runtime_config_payload(
                num_steps=self._server.num_steps,
                fps=fps,
                timeline_fps=self._server._timeline_fps,
                loop=loop,
                timeline_slider_uuid=impl.gui_uuid(self._timeline_slider),
                fps_slider_uuid=impl.gui_uuid(self._fps_slider),
                step_buttons_uuid=impl.gui_uuid(self._step_buttons),
                play_button_uuid=impl.gui_uuid(self._play_button),
                pause_button_uuid=impl.gui_uuid(self._pause_button),
                playback_state_sync_uuid=impl.gui_uuid(self._playback_state_sync),
                timestep_sync_uuid=impl.gui_uuid(self._timestep_sync),
            ),
        )
        self._timeline_slider.max = max(self._server.num_steps - 1, 0)
        self._timestep_sync.max = max(self._server.num_steps - 1, 0)

    def apply_theme_colors(self, brand_color: tuple[int, int, int] | None) -> None:
        """Apply the current theme color to the playback buttons."""
        self._play_button.color = None
        self._pause_button.color = _pause_button_color(brand_color)

    def _sync_from_client(self, timestep: int) -> None:
        assert 0 <= timestep < self._server.num_steps
        with self._lock:
            self._current_timestep = timestep
        self._server._dispatch_timestep_change(self._client, timestep)

    def _sync_playback_from_client(self, is_playing: bool) -> None:
        with self._lock:
            if is_playing == self._is_playing:
                return
            self._is_playing = is_playing
        self._server._dispatch_playback_change(self._client, is_playing)

    def _set_current_timestep(self, timestep: int) -> None:
        assert 0 <= timestep < self._server.num_steps
        with self._lock:
            self._current_timestep = timestep
        self._timeline_slider.value = timestep

    def _send_runtime_call(
        self, method: RuntimeMethod, payload: RuntimePayload
    ) -> None:
        message = make_runtime_message(method, payload)
        impl.queue_client_message(self._client, message)

    def _set_fps_slider_value(self, fps: float) -> None:
        if self._fps_slider.value == fps:
            return
        self._fps_slider.value = fps


def _pause_button_color(
    brand_color: tuple[int, int, int] | None,
) -> tuple[int, int, int]:
    base_color = _DEFAULT_PRIMARY_COLOR if brand_color is None else brand_color
    return (
        int(base_color[0] * 0.85),
        int(base_color[1] * 0.85),
        int(base_color[2] * 0.85),
    )
