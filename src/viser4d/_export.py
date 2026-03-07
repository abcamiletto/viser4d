from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING

from viser import _messages

from . import _viser_private as impl
from ._runtime import RUNTIME_MARKER, clamp, make_runtime_message, runtime_source
from ._timeline import TimelineStore, serialize_viser_messages

if TYPE_CHECKING:
    from ._server import Viser4dServer


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
        if start > end:
            raise ValueError(
                f"Invalid timestep range: start_timestep={start_timestep}, "
                f"end_timestep={end_timestep}."
            )
        export_num_steps = end - start + 1
        export_step = clamp(
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
        return clamp(int(end_timestep), 0, self._server.num_steps - 1)

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
                self._server._controller.runtime_config_payload(
                    num_steps=export_num_steps
                ),
            )
        )
        messages.extend(self._timeline_messages(start=start, end=end))
        messages.extend(self._baseline_messages())
        messages.extend(
            self._gui_messages(
                export_num_steps=export_num_steps,
                export_step=export_step,
            )
        )
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

    def _gui_messages(
        self,
        *,
        export_num_steps: int,
        export_step: int,
    ) -> list[_messages.Message]:
        server = self._server
        return [
            _messages.GuiUpdateMessage(
                impl.gui_uuid(server._timestep_sync),
                {"value": export_step, "max": export_num_steps - 1},
            ),
            _messages.GuiUpdateMessage(
                impl.gui_uuid(server._timeline_slider),
                {"value": export_step, "max": export_num_steps - 1},
            ),
            _messages.GuiUpdateMessage(
                impl.gui_uuid(server._play_button),
                {"visible": True},
            ),
            _messages.GuiUpdateMessage(
                impl.gui_uuid(server._pause_button),
                {"visible": False},
            ),
            make_runtime_message("seek", {"step": export_step}),
        ]
