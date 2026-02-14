from __future__ import annotations

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
        delta = -1 if event.target.value == "Prev" else 1
        current = int(self._slider.value)
        next_step = max(0, min(self._server.num_steps - 1, current + delta))
        if next_step != current:
            self._server.seek(next_step)

    def _on_play_button(self, _event) -> None:
        """Handle play/pause button clicks."""
        next_playing = not self._playing
        self.set_playing(next_playing)

        loop = self._server.get_event_loop()
        if next_playing:
            loop.call_soon_threadsafe(self._server.play, self._fps_slider.value)
        else:
            loop.call_soon_threadsafe(self._server.pause)

    def _on_fps(self, event) -> None:
        """Handle FPS slider changes."""
        if self._suppress_fps:
            return
        self._server._set_fps(event.target.value)
