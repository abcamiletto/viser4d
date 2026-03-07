from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from viser import _messages
from viser._icons import svg_from_icon
from viser._icons_enum import Icon
from viser._messages import GuiButtonProps, GuiFolderProps, GuiSliderProps

from . import _viser_private as impl
from ._runtime import RUNTIME_MARKER, clamp, make_runtime_message, runtime_source
from ._timeline import TimelineStore, serialize_viser_messages

if TYPE_CHECKING:
    from viser._viser import ClientHandle

    from ._server import Viser4dServer


EXPORT_FOLDER_UUID = "__viser4d_export_playback_folder__"
EXPORT_TIMESTEP_UUID = "__viser4d_export_timestep__"
EXPORT_PLAY_UUID = "__viser4d_export_play__"
EXPORT_PAUSE_UUID = "__viser4d_export_pause__"


class ExportBuilder:
    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._timeline = timeline

    def serialize(
        self,
        path: str | pathlib.Path,
        *,
        client: ClientHandle | None = None,
        start_timestep: int = 0,
        end_timestep: int = -1,
    ) -> bytes:
        start = max(0, int(start_timestep))
        end = self._normalized_end_timestep(end_timestep)
        if start > end:
            raise ValueError(
                f"Invalid timestep range: start_timestep={start_timestep}, "
                f"end_timestep={end_timestep}."
            )
        export_num_steps = end - start + 1
        export_step = clamp(
            self._source_timestep(client) - start,
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
        return clamp(int(end_timestep), 0, self._server.num_steps - 1)

    def _source_timestep(self, client: ClientHandle | None) -> int:
        if client is None:
            return self._server._current_timestep
        return client.playback.current_timestep

    def _build_messages(
        self,
        *,
        start: int,
        end: int,
        export_num_steps: int,
        export_step: int,
    ) -> list[_messages.Message]:
        messages = self._base_messages()
        messages.append(_messages.RunJavascriptMessage(runtime_source()))
        messages.append(
            make_runtime_message(
                "configure",
                self._server._runtime_config_payload(num_steps=export_num_steps),
            )
        )
        messages.extend(self._timeline_messages(start=start, end=end))
        messages.extend(self._baseline_messages())
        messages.extend(
            self._playback_gui_messages(
                export_num_steps=export_num_steps,
                export_step=export_step,
            )
        )
        messages.append(make_runtime_message("seek", {"step": export_step}))
        return messages

    def _base_messages(self) -> list[_messages.Message]:
        messages: list[_messages.Message] = []
        for message in impl.broadcast_messages(self._server):
            if isinstance(message, _messages.RunJavascriptMessage) and (
                message.source.startswith(RUNTIME_MARKER)
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
                    make_runtime_message(
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
                    make_runtime_message(
                        "preloadAudioStep",
                        {"step": export_index, "ops": step_state.audio_ops},
                    )
                )
        return messages

    def _baseline_messages(self) -> list[_messages.Message]:
        return [
            make_runtime_message(
                "setBaseline",
                {"name": name, "messages": baseline},
            )
            for name, baseline in self._timeline.baseline_messages_by_name.items()
        ]

    def _playback_gui_messages(
        self, *, export_num_steps: int, export_step: int
    ) -> list[_messages.Message]:
        return [
            _messages.GuiFolderMessage(
                uuid=EXPORT_FOLDER_UUID,
                container_uuid="root",
                props=GuiFolderProps(
                    order=1.0,
                    label="Playback",
                    visible=True,
                    expand_by_default=True,
                ),
            ),
            _messages.GuiSliderMessage(
                value=export_step,
                uuid=EXPORT_TIMESTEP_UUID,
                container_uuid=EXPORT_FOLDER_UUID,
                props=GuiSliderProps(
                    order=1.0,
                    label="Timestep",
                    hint=None,
                    min=0,
                    max=max(export_num_steps - 1, 0),
                    step=1,
                    precision=0,
                    visible=True,
                    disabled=False,
                    _marks=None,
                ),
            ),
            _messages.GuiButtonMessage(
                value=False,
                uuid=EXPORT_PLAY_UUID,
                container_uuid=EXPORT_FOLDER_UUID,
                props=GuiButtonProps(
                    order=2.0,
                    label="Play",
                    hint=None,
                    visible=True,
                    disabled=False,
                    color="green",
                    _icon_html=svg_from_icon(Icon.PLAYER_PLAY_FILLED),
                    _hold_callback_freqs=(),
                ),
            ),
            _messages.GuiButtonMessage(
                value=False,
                uuid=EXPORT_PAUSE_UUID,
                container_uuid=EXPORT_FOLDER_UUID,
                props=GuiButtonProps(
                    order=3.0,
                    label="Pause",
                    hint=None,
                    visible=False,
                    disabled=False,
                    color="yellow",
                    _icon_html=svg_from_icon(Icon.PLAYER_PAUSE_FILLED),
                    _hold_callback_freqs=(),
                ),
            ),
        ]
