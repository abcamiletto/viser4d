from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import Viser4dServer


class PlaybackControls:
    """GUI controls for timeline playback.

    Registers itself as a timestep listener and manages its own state based on
    user interactions and timestep callbacks.
    """

    def __init__(self, server: Viser4dServer) -> None:
        self._server = server
        self._playing = False
        self._suppress_fps = False
        self._play_request_lock = threading.Lock()
        self._play_request_id = 0

        with server.gui.add_folder("Playback"):
            self._play_button = server.gui.add_button("Play", order=0)
            self._slider = server.gui.add_slider(
                "Timestep",
                min=0,
                max=server.num_steps - 1,
                step=1,
                initial_value=0,
                order=1,
            )
            self._step_buttons = server.gui.add_button_group(
                "",
                ("Prev", "Next"),
                order=2,
            )
            self._fps_slider = server.gui.add_slider(
                "FPS",
                min=1.0,
                max=60.0,
                step=1.0,
                initial_value=server._fps,
                order=3,
            )

        self._play_button.on_click(self._on_play_button)
        self._step_buttons.on_click(self._on_step)
        self._slider.on_update(self._on_slider)
        self._fps_slider.on_update(self._on_fps)

        # Register as timestep listener
        server.on_timestep_change(self._on_timestep)

    def _on_timestep(self, t: int) -> None:
        """Handle timestep changes from the server."""
        # Server timeline state is authoritative for slider position.
        self._slider.value = t

        # Detect playback ended (reached last frame while playing, non-looping)
        if self._playing and t == self._server.num_steps - 1:
            # Check if playback actually stopped (not looping)
            # We'll get another callback if it loops, so defer the check
            pass  # Handled by server calling set_playing(False)

    def set_fps(self, fps: float) -> None:
        """Update the FPS slider value."""
        if self._fps_slider.value == fps:
            return
        self._suppress_fps = True
        self._fps_slider.value = fps
        self._suppress_fps = False

    def set_playing(self, playing: bool) -> None:
        """Update the play/pause button state."""
        if self._playing == playing:
            return
        self._playing = playing
        self._play_button.label = "Pause" if playing else "Play"

    def _on_slider(self, event) -> None:
        """Handle user dragging the timestep slider."""
        # Ignore server-originated updates to avoid feedback loops.
        if event.client_id is None:
            return
        self._server.seek(int(event.target.value))

    def _on_step(self, event) -> None:
        """Handle prev/next button clicks."""
        current = self._slider.value
        if event.target.value == "Prev":
            if current > 0:
                self._server.seek(current - 1)
        else:
            if current < self._server.num_steps - 1:
                self._server.seek(current + 1)

    def _on_play_button(self, _event) -> None:
        """Handle play/pause button clicks."""
        if self._playing:
            next_playing = False
        else:
            next_playing = True

        self._playing = next_playing
        self._play_button.label = "Pause" if next_playing else "Play"

        with self._play_request_lock:
            self._play_request_id += 1
            request_id = self._play_request_id

        loop = self._server.get_event_loop()
        loop.call_soon_threadsafe(
            lambda: loop.create_task(
                self._apply_play_request(
                    request_id, next_playing, self._fps_slider.value
                )
            )
        )

    async def _apply_play_request(
        self, request_id: int, next_playing: bool, fps: float
    ) -> None:
        """Apply a play-state change without blocking the GUI event loop."""
        with self._play_request_lock:
            if request_id != self._play_request_id:
                return

        if next_playing:
            await asyncio.to_thread(self._server.play, fps)
        else:
            await asyncio.to_thread(self._server.pause)

    def _on_fps(self, event) -> None:
        """Handle FPS slider changes."""
        if self._suppress_fps:
            return
        self._server._set_fps(event.target.value)
