from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import ViserServer


class PlaybackControls:
    def __init__(self, server: ViserServer) -> None:
        self._server = server
        self._playing = False
        self._suppress = False

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

    def set_time(self, t: int) -> None:
        if self._slider.value == t:
            return
        self._suppress = True
        self._slider.value = t
        self._suppress = False

    def set_fps(self, fps: float) -> None:
        if self._fps_slider.value == fps:
            return
        self._suppress = True
        self._fps_slider.value = fps
        self._suppress = False

    def set_playing(self, playing: bool) -> None:
        if self._playing == playing:
            return
        self._playing = playing
        self._play_button.label = "Pause" if playing else "Play"

    def _on_slider(self, event) -> None:
        if self._suppress:
            return
        self._server.seek(event.target.value)

    def _on_step(self, event) -> None:
        current = self._slider.value
        if event.target.value == "Prev":
            if current > 0:
                self._server.seek(current - 1)
        else:
            if current < self._server.num_steps - 1:
                self._server.seek(current + 1)

    def _on_play_button(self, _event) -> None:
        if self._playing:
            self._server.pause()
        else:
            self._server.play(self._fps_slider.value)

    def _on_fps(self, event) -> None:
        if self._suppress:
            return
        self._server._set_fps(event.target.value)
