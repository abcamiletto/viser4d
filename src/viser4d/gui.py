from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .playback import PlaybackController


class PlaybackControls:
    """GUI controls for timeline playback (transport listener + timestep callback)."""

    def __init__(self, gui: Any, controller: PlaybackController) -> None:
        self._controller = controller
        self._suppress_fps = False

        with gui.add_folder("Playback"):
            self._play_button = gui.add_button("Play", order=0)
            self._slider = gui.add_slider(
                "Timestep",
                min=0,
                max=controller.num_steps - 1,
                step=1,
                initial_value=0,
                order=1,
            )
            self._step_buttons = gui.add_button_group("", ("Prev", "Next"), order=2)
            self._fps_slider = gui.add_slider(
                "FPS",
                min=1.0,
                max=60.0,
                step=1.0,
                initial_value=controller.fps,
                order=3,
            )

        self._play_button.on_click(self._on_play_button)
        self._step_buttons.on_click(self._on_step)
        self._slider.on_update(self._on_slider)
        self._fps_slider.on_update(self._on_fps)

    # Transport listener methods

    def on_play(self, step: int, fps: float) -> None:
        self._play_button.label = "Pause"

    def on_pause(self, step: int) -> None:
        self._play_button.label = "Play"

    def on_seek(self, step: int, fps: float) -> None:
        self.on_pause(step)

    def on_fps_change(self, fps: float, step: int) -> None:
        if self._fps_slider.value == fps:
            return
        self._suppress_fps = True
        self._fps_slider.value = fps
        self._suppress_fps = False

    def _on_timestep(self, t: int) -> None:
        self._slider.value = t

    # Widget event handlers

    def _on_slider(self, event) -> None:
        if event.client_id is None:
            return
        self._controller.seek(int(event.target.value))

    def _on_step(self, event) -> None:
        delta = -1 if event.target.value == "Prev" else 1
        current = int(self._slider.value)
        next_step = max(0, min(self._controller.num_steps - 1, current + delta))
        if next_step != current:
            self._controller.seek(next_step)

    def _on_play_button(self, _event) -> None:
        if self._controller.is_playing():
            self._controller.pause()
        else:
            self._controller.play(self._fps_slider.value)

    def _on_fps(self, event) -> None:
        if self._suppress_fps:
            return
        self._controller.set_fps(event.target.value)
