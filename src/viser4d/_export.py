from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING, cast

from . import _viser_private as impl
from ._protocol import SerializedMessage
from ._runtime import RUNTIME_MARKER
from ._timeline import TimelineStore, serialize_viser_recording, to_jsonable

if TYPE_CHECKING:
    from ._server import Viser4dServer


class ExportBuilder:
    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._timeline = timeline

    def serialize(
        self, *, start_timestep: int = 0, end_timestep: int | None = None
    ) -> bytes:
        start = int(start_timestep)
        end = self._server.num_steps - 1 if end_timestep is None else int(end_timestep)
        assert 0 <= start < self._server.num_steps
        assert 0 <= end < self._server.num_steps
        assert start <= end

        recording: list[tuple[float, SerializedMessage]] = []
        for message in impl.broadcast_messages(self._server):
            if getattr(message, "source", "").startswith(RUNTIME_MARKER):
                continue
            if getattr(message, "name", None) in self._timeline.node_names:
                continue
            recording.append(
                (
                    0.0,
                    cast(SerializedMessage, to_jsonable(message.as_serializable_dict())),
                )
            )
        for baseline in self._timeline.baseline_messages_by_name.values():
            recording.extend(
                (
                    0.0,
                    cast(SerializedMessage, to_jsonable(message.as_serializable_dict())),
                )
                for message in baseline
            )
        fps = max(self._server._base_fps, 1.0)
        for step in range(end + 1):
            time = 0.0 if step <= start else (step - start) / fps
            recording.extend(
                (
                    time,
                    cast(SerializedMessage, to_jsonable(message.as_serializable_dict())),
                )
                for message in self._timeline.step(step).messages
            )

        blob = serialize_viser_recording(
            recording,
            duration_seconds=max(end - start, 0) / fps,
        )
        return blob

    def write(
        self,
        path: str | pathlib.Path,
        *,
        start_timestep: int = 0,
        end_timestep: int | None = None,
    ) -> None:
        blob = self.serialize(
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        )
        pathlib.Path(path).write_bytes(blob)
