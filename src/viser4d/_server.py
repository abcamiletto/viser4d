from __future__ import annotations

import contextlib
import json
import pathlib
import threading
import time
from typing import Any, Callable, Iterator

import numpy as np
import viser
from viser import _messages

from ._audio import AudioHandle, AudioState, audio_array_payload
from ._timeline import (
    TimelineRecorder,
    TimelineStore,
    is_scene_message,
    serialize_viser_messages,
    to_jsonable,
)
from ._viser_private import (
    broadcast_messages,
    gui_uuid,
    scene_recording_interface,
)


_RUNTIME_MARKER = "/*__VISER4D_RUNTIME__*/"


def _runtime_source() -> str:
    return _RUNTIME_MARKER + (pathlib.Path(__file__).resolve().parent / "runtime.js").read_text()


def _clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))


def _runtime_config_payload(
    *,
    num_steps: int,
    fps: float,
    base_fps: float,
    loop: bool,
    timestep_sync_uuid: str,
) -> dict[str, Any]:
    return {
        "numSteps": num_steps,
        "fps": fps,
        "baseFps": base_fps,
        "loop": loop,
        "timestepSyncUuid": timestep_sync_uuid,
    }


def _make_runtime_message(
    method: str,
    payload: dict[str, Any],
) -> _messages.RunJavascriptMessage:
    source = _RUNTIME_MARKER + f"""
(() => {{
  const invoke = () => {{
    if (!window.__VISER4D__) return false;
    window.__VISER4D__.{method}({json.dumps(payload)});
    return true;
  }};
  if (invoke()) return;
  const timer = window.setInterval(() => {{
    if (!invoke()) return;
    window.clearInterval(timer);
  }}, 50);
}})();
"""
    return _messages.RunJavascriptMessage(source)


class TimelineController:
    def __init__(self, server: Viser4dServer, *, fps: float) -> None:
        self._server = server
        self._fps = float(fps)
        self._base_fps = float(fps)
        self._loop = False
        self._is_playing = False
        self._current_timestep = 0
        self._syncing_timestep_slider = False
        self._callbacks: list[Callable[[int], None]] = []
        self._lock = threading.RLock()
        self._anchor_step = 0.0
        self._anchor_time = time.monotonic()
        self._predictor_thread = threading.Thread(
            target=self._predictor_loop,
            name="viser4d-timeline-predictor",
            daemon=True,
        )
        self._predictor_thread.start()

    @property
    def fps(self) -> float:
        return self._fps

    @property
    def loop(self) -> bool:
        return self._loop

    @property
    def is_playing(self) -> bool:
        return self._is_playing

    @property
    def current_timestep(self) -> int:
        return self._current_timestep

    @property
    def syncing_timestep_slider(self) -> bool:
        return self._syncing_timestep_slider

    def play(self, fps: float, loop: bool = False) -> None:
        with self._lock:
            current_step = self._transport_step()
            self._fps = float(fps)
            self._loop = bool(loop)
            self._is_playing = True
            self._set_anchor(current_step)
        self._sync_playback_buttons()
        self._server._send_runtime_call("play", {"fps": self._fps, "loop": self._loop})

    def pause(self) -> None:
        with self._lock:
            self._set_anchor(self._transport_step())
            self._is_playing = False
        self._sync_playback_buttons()
        self._server._send_runtime_call("pause", {})

    def seek(self, t: int) -> None:
        timestep = _clamp(int(t), 0, self._server.num_steps - 1)
        with self._lock:
            self._set_anchor(float(timestep))
        self.set_current_timestep(timestep)
        self._server._send_runtime_call("seek", {"step": timestep})

    def on_timestep_change(self, callback: Callable[[int], None]) -> None:
        self._callbacks.append(callback)

    def sync_from_client(self, timestep: int) -> None:
        timestep = _clamp(int(timestep), 0, self._server.num_steps - 1)
        should_sync_buttons = False
        with self._lock:
            if self._is_playing and not self._loop and timestep >= self._server.num_steps - 1:
                self._set_anchor(float(timestep))
                self._is_playing = False
                should_sync_buttons = True
            elif self._is_playing:
                predicted = self._transport_step()
                error = float(timestep) - predicted
                if self._loop and self._server.num_steps > 0:
                    error = (
                        (error + self._server.num_steps / 2) % self._server.num_steps
                    ) - self._server.num_steps / 2
                correction = error if abs(error) > 2.0 else error * 0.35
                self._set_anchor(predicted + correction)
                should_sync_buttons = False
            else:
                self._set_anchor(float(timestep))
                should_sync_buttons = False
            is_playing = self._is_playing
        if should_sync_buttons:
            self._sync_playback_buttons()
        if not is_playing:
            self.set_current_timestep(timestep)

    def set_fps(self, fps: float) -> None:
        with self._lock:
            current_step = self._transport_step()
            self._fps = float(fps)
            self._set_anchor(current_step)
            is_playing = self._is_playing
        if is_playing:
            self._server._send_runtime_call(
                "setFps",
                {"fps": self._fps, "loop": self._loop},
            )
            return
        self.sync_runtime_config()

    def sync_runtime_config(self, *, num_steps: int | None = None) -> None:
        self._server._send_runtime_call(
            "configure",
            self.runtime_config_payload(num_steps=num_steps),
        )

    def runtime_config_payload(self, *, num_steps: int | None = None) -> dict[str, Any]:
        return _runtime_config_payload(
            num_steps=self._server.num_steps if num_steps is None else num_steps,
            fps=self._fps,
            base_fps=self._base_fps,
            loop=self._loop,
            timestep_sync_uuid=gui_uuid(self._server._timestep_sync),
        )

    def set_current_timestep(self, timestep: int) -> None:
        timestep = _clamp(timestep, 0, self._server.num_steps - 1)
        if timestep == self._current_timestep:
            return
        self._current_timestep = timestep
        self._syncing_timestep_slider = True
        try:
            self._server._timeline_slider.value = timestep
        finally:
            self._syncing_timestep_slider = False
        for callback in list(self._callbacks):
            callback(timestep)

    def _sync_playback_buttons(self) -> None:
        self._server._play_button.visible = not self._is_playing
        self._server._pause_button.visible = self._is_playing

    def _set_anchor(self, step: float, *, now: float | None = None) -> None:
        self._anchor_step = max(0.0, min(float(step), self._server.num_steps - 1))
        self._anchor_time = time.monotonic() if now is None else now

    def _transport_step(self, *, now: float | None = None) -> float:
        now = time.monotonic() if now is None else now
        if not self._is_playing:
            return self._anchor_step
        step = self._anchor_step + (now - self._anchor_time) * self._fps
        if self._loop and self._server.num_steps > 0:
            return step % self._server.num_steps
        return max(0.0, min(step, self._server.num_steps - 1))

    def _predictor_loop(self) -> None:
        while True:
            time.sleep(0.05)
            with self._lock:
                is_playing = self._is_playing
                timestep = int(self._transport_step())
            if not is_playing:
                continue
            self.set_current_timestep(timestep)


class SceneRecorder:
    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._timeline = timeline
        self._active_step: int | None = None

    @property
    def active_step(self) -> int | None:
        return self._active_step

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[None]:
        step = self._timeline.validate_step(int(t))
        recorder = TimelineRecorder()
        self._active_step = step
        try:
            with scene_recording_interface(self._server._live_scene, recorder):
                yield
        finally:
            self._active_step = None

        if not recorder.messages:
            return

        serializable_messages = [
            to_jsonable(message.as_serializable_dict()) for message in recorder.messages
        ]
        step_store = self._timeline.record_scene_messages(step, serializable_messages)
        for node_name in step_store.node_names:
            self._register_timeline_node(node_name)

        self._server._send_runtime_call(
            "preloadSceneStep",
            {
                "step": step,
                "messages": serializable_messages,
                "nodeNames": sorted(step_store.node_names),
            },
        )

    def add_audio(
        self,
        name: str,
        *,
        data: np.ndarray,
        sample_rate: int,
    ) -> AudioHandle:
        state = AudioState(
            name=name,
            sample_rate=int(sample_rate),
            waveform=np.ascontiguousarray(data),
        )
        handle = AudioHandle(self._server, state)
        assert self._active_step is not None
        op = {
            "op": "add",
            "name": name,
            "sampleRate": state.sample_rate,
            "waveform": audio_array_payload(state.waveform),
            "volume": state.volume,
        }
        self._timeline.record_audio_ops(self._active_step, [op])
        self._server._send_runtime_call(
            "preloadAudioStep",
            {"step": self._active_step, "ops": [op]},
        )
        return handle

    def dispatch_audio_update(self, op: dict[str, Any]) -> None:
        if self._active_step is not None:
            self._timeline.record_audio_ops(self._active_step, [op])
            self._server._send_runtime_call(
                "preloadAudioStep",
                {"step": self._active_step, "ops": [op]},
            )
            return
        self._server._send_runtime_call("applyAudioUpdate", op)

    def _register_timeline_node(self, name: str) -> None:
        if name in self._timeline.baseline_messages_by_name:
            return
        baseline = self._collect_live_messages_for_name(name)
        if not baseline:
            return
        self._timeline.baseline_messages_by_name[name] = baseline
        self._server._send_runtime_call(
            "setBaseline",
            {"name": name, "messages": baseline},
        )

    def _collect_live_messages_for_name(self, name: str) -> list[dict[str, Any]]:
        return [
            to_jsonable(message.as_serializable_dict())
            for message in broadcast_messages(self._server)
            if is_scene_message(message) and getattr(message, "name", None) == name
        ]


class ExportBuilder:
    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._timeline = timeline

    def serialize(
        self,
        path: str | pathlib.Path,
        *,
        start_timestep: int = 0,
        end_timestep: int = -1,
    ) -> bytes:
        start = max(0, int(start_timestep))
        end = self._normalized_end_timestep(end_timestep)
        export_num_steps = end - start + 1
        export_step = _clamp(
            self._server._controller.current_timestep - start,
            0,
            export_num_steps - 1,
        )
        messages = self._build_messages(
            start=start,
            end=end,
            export_num_steps=export_num_steps,
            export_step=export_step,
        )
        blob = serialize_viser_messages(messages)
        pathlib.Path(path).write_bytes(blob)
        return blob

    def _normalized_end_timestep(self, end_timestep: int) -> int:
        if end_timestep < 0:
            return self._server.num_steps - 1
        return _clamp(int(end_timestep), 0, self._server.num_steps - 1)

    def _build_messages(
        self,
        *,
        start: int,
        end: int,
        export_num_steps: int,
        export_step: int,
    ) -> list[_messages.Message]:
        messages = self._base_messages()
        messages.append(_messages.RunJavascriptMessage(_runtime_source()))
        messages.append(
            _make_runtime_message(
                "configure",
                self._server._controller.runtime_config_payload(num_steps=export_num_steps),
            )
        )
        messages.extend(self._timeline_messages(start=start, end=end))
        messages.extend(self._baseline_messages())
        messages.extend(self._gui_messages(export_num_steps=export_num_steps, export_step=export_step))
        return messages

    def _base_messages(self) -> list[_messages.Message]:
        messages: list[_messages.Message] = []
        for message in broadcast_messages(self._server):
            if (
                isinstance(message, _messages.RunJavascriptMessage)
                and message.source.startswith(_RUNTIME_MARKER)
            ):
                continue
            messages.append(message)
        return messages

    def _timeline_messages(self, *, start: int, end: int) -> list[_messages.Message]:
        messages: list[_messages.Message] = []
        for export_index, step in enumerate(range(start, end + 1)):
            step_state = self._timeline.step(step)
            if step_state.messages:
                messages.append(
                    _make_runtime_message(
                        "preloadSceneStep",
                        {
                            "step": export_index,
                            "messages": step_state.messages,
                            "nodeNames": sorted(step_state.node_names),
                        },
                    )
                )
            if step_state.audio_ops:
                messages.append(
                    _make_runtime_message(
                        "preloadAudioStep",
                        {"step": export_index, "ops": step_state.audio_ops},
                    )
                )
        return messages

    def _baseline_messages(self) -> list[_messages.Message]:
        return [
            _make_runtime_message(
                "setBaseline",
                {"name": name, "messages": baseline},
            )
            for name, baseline in self._timeline.baseline_messages_by_name.items()
        ]

    def _gui_messages(
        self,
        *,
        export_num_steps: int,
        export_step: int,
    ) -> list[_messages.Message]:
        server = self._server
        return [
            _messages.GuiUpdateMessage(
                gui_uuid(server._timestep_sync),
                {"value": export_step, "max": export_num_steps - 1},
            ),
            _messages.GuiUpdateMessage(
                gui_uuid(server._timeline_slider),
                {"value": export_step, "max": export_num_steps - 1},
            ),
            _messages.GuiUpdateMessage(gui_uuid(server._play_button), {"visible": True}),
            _messages.GuiUpdateMessage(gui_uuid(server._pause_button), {"visible": False}),
            _make_runtime_message("seek", {"step": export_step}),
        ]


class _SceneFacade:
    def __init__(self, server: Viser4dServer, live_scene: viser.SceneApi):
        self._server = server
        self._live_scene = live_scene

    def __getattr__(self, name: str) -> Any:
        return getattr(self._live_scene, name)

    def add_audio(
        self, name: str, *, data: np.ndarray, sample_rate: int
    ) -> AudioHandle:
        if self._server._recorder.active_step is None:
            raise RuntimeError("scene.add_audio() is only valid inside server.at(t).")
        return self._server._add_audio(name, data=data, sample_rate=sample_rate)


class Viser4dServer(viser.ViserServer):
    def __init__(self, num_steps: int, fps: float = 30.0, **kwargs) -> None:
        super().__init__(**kwargs)

        self.num_steps = int(num_steps)
        self._timeline = TimelineStore(self.num_steps)
        self._live_scene = self.scene
        self.scene = _SceneFacade(self, self._live_scene)
        self._controller = TimelineController(self, fps=fps)
        self._recorder = SceneRecorder(self, self._timeline)
        self._export_builder = ExportBuilder(self, self._timeline)

        self._websock_server.queue_message(
            _messages.RunJavascriptMessage(_runtime_source())
        )
        self._timestep_sync = self.gui.add_number(
            "__viser4d_timestep_sync__",
            0,
            min=0,
            max=max(self.num_steps - 1, 0),
            step=1,
            visible=False,
        )
        with self.gui.add_folder("Playback"):
            self._timeline_slider = self.gui.add_slider(
                "Timestep",
                min=0,
                max=max(self.num_steps - 1, 0),
                step=1,
                initial_value=0,
            )
            self._fps_slider = self.gui.add_slider(
                "FPS",
                min=1.0,
                max=120.0,
                step=1.0,
                initial_value=self._controller.fps,
            )
            self._step_buttons = self.gui.add_button_group("Step", ("Prev", "Next"))
            self._play_button = self.gui.add_button(
                "Play",
                color="green",
                icon=viser.Icon.PLAYER_PLAY_FILLED,
            )
            self._pause_button = self.gui.add_button(
                "Pause",
                color="yellow",
                icon=viser.Icon.PLAYER_PAUSE_FILLED,
                visible=False,
            )

        @self._timestep_sync.on_update
        def _sync(_event: Any) -> None:
            self._controller.sync_from_client(int(self._timestep_sync.value))

        @self._timeline_slider.on_update
        def _sync_slider(_event: Any) -> None:
            if self._controller.syncing_timestep_slider:
                return
            self.seek(int(self._timeline_slider.value))

        @self._fps_slider.on_update
        def _sync_fps(_event: Any) -> None:
            self._controller.set_fps(float(self._fps_slider.value))

        @self._step_buttons.on_click
        def _step(_event: Any) -> None:
            if self._step_buttons.value == "Prev":
                self.seek(self._controller.current_timestep - 1)
            else:
                self.seek(self._controller.current_timestep + 1)

        @self._play_button.on_click
        def _play(_event: Any) -> None:
            self.play(self._controller.fps, loop=self._controller.loop)

        @self._pause_button.on_click
        def _pause(_event: Any) -> None:
            self.pause()
        self._controller.sync_runtime_config()

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[None]:
        with self._recorder.at(t):
            yield

    def play(self, fps: float, loop: bool = False) -> None:
        self._controller.play(fps, loop=loop)

    def pause(self) -> None:
        self._controller.pause()

    def seek(self, t: int) -> None:
        self._controller.seek(t)

    def on_timestep_change(self, callback: Callable[[int], None]) -> None:
        self._controller.on_timestep_change(callback)

    def sleep_forever(self) -> None:
        while True:
            time.sleep(3600)

    def serialize(
        self,
        path: str | pathlib.Path,
        *,
        start_timestep: int = 0,
        end_timestep: int = -1,
    ) -> bytes:
        return self._export_builder.serialize(
            path,
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        )

    def _add_audio(self, name: str, *, data: np.ndarray, sample_rate: int) -> AudioHandle:
        return self._recorder.add_audio(name, data=data, sample_rate=sample_rate)

    def _dispatch_audio_update(self, _name: str, op: dict[str, Any]) -> None:
        self._recorder.dispatch_audio_update(op)

    def _send_runtime_call(self, method: str, payload: dict[str, Any]) -> None:
        self._websock_server.queue_message(_make_runtime_message(method, payload))

    @property
    def _fps(self) -> float:
        return self._controller.fps

    @property
    def _loop(self) -> bool:
        return self._controller.loop

    @property
    def _is_playing(self) -> bool:
        return self._controller.is_playing

    @property
    def _current_timestep(self) -> int:
        return self._controller.current_timestep
