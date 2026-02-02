from typing import Callable

import viser as _viser


class PlaybackControls:
    def __init__(
        self,
        gui: _viser.GuiApi,
        num_steps: int,
        fps: float,
        *,
        on_seek: Callable[[int], None],
        on_play: Callable[[], None],
        on_pause: Callable[[], None],
        on_fps: Callable[[float], None],
    ) -> None:
        self._on_seek = on_seek
        self._on_play = on_play
        self._on_pause = on_pause
        self._on_fps_change = on_fps
        self._playing = False
        self._suppress = False
        self._num_steps = num_steps

        with gui.add_folder("Playback"):
            self.play_button = gui.add_button("Play", order=0)
            self.slider = gui.add_slider(
                "Timestep",
                min=0,
                max=num_steps - 1,
                step=1,
                initial_value=0,
                order=1,
            )
            self.step_buttons = gui.add_button_group(
                "",
                ("Prev", "Next"),
                order=2,
            )
            self.fps_slider = gui.add_slider(
                "FPS",
                min=1.0,
                max=60.0,
                step=1.0,
                initial_value=fps,
                order=3,
            )

        self.play_button.on_click(self._on_play_button)
        self.step_buttons.on_click(self._on_step)
        self.slider.on_update(self._on_slider)
        self.fps_slider.on_update(self._on_fps)

    def set_time(self, t: int) -> None:
        if self.slider.value == t:
            return
        self._suppress = True
        self.slider.value = t
        self._suppress = False

    def set_fps(self, fps: float) -> None:
        if self.fps_slider.value == fps:
            return
        self._suppress = True
        self.fps_slider.value = fps
        self._suppress = False

    def seek(self, t: int) -> None:
        self.set_time(t)
        self._on_seek(t)

    def set_playing(self, playing: bool) -> None:
        if self._playing == playing:
            return
        self._playing = playing
        self.play_button.label = "Pause" if playing else "Play"

    def _on_slider(self, event) -> None:
        if self._suppress:
            return
        self._on_seek(event.target.value)

    def _on_prev(self) -> None:
        current = self.slider.value
        if current > 0:
            current -= 1
        self.seek(current)

    def _on_next(self) -> None:
        current = self.slider.value
        if current < self._num_steps - 1:
            current += 1
        self.seek(current)

    def _on_step(self, event) -> None:
        if event.target.value == "Prev":
            self._on_prev()
        else:
            self._on_next()

    def _on_play_button(self, _event) -> None:
        if self._playing:
            self._on_pause()
        else:
            self._on_play()

    def _on_fps(self, event) -> None:
        if self._suppress:
            return
        self._on_fps_change(event.target.value)
