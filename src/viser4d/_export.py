from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING

import viser.infra
from rich.progress import track

from . import _viser_private as impl
from ._hybrid import stored_message_as_serializable_dict
from ._types import StoredMessage
from ._runtime import RUNTIME_MARKER, runtime_source
from .timeline._messages_util import (
    serialize_viser_embed_recording,
    store_raw_message,
)
from .timeline._store import TimelineStore

if TYPE_CHECKING:
    from ._server import Viser4dServer


class ExportBuilder:
    """Serialize the current timeline into viser's recording formats."""

    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._timeline = timeline

    def serialize(
        self, *, start_timestep: int = 0, end_timestep: int | None = None
    ) -> bytes:
        """Build a native `.viser` scene recording for the requested timestep range."""
        start, end = self._validate_timesteps(
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        )
        serializer = self._create_native_serializer()
        for message in self._static_messages():
            serializer._insert_message(message)

        fps = self._server.fps
        binary_buffers = serializer._binary_buffers
        recorded_messages = serializer._messages
        for step in track(
            range(end + 1),
            description="Exporting .viser",
            disable=not sys.stdout.isatty(),
            transient=True,
        ):
            if step > start:
                serializer.insert_sleep(1.0 / fps)
            step_state = self._timeline.step(step)
            for message in step_state.scene_updates.values():
                recorded_messages.append(
                    (
                        serializer._time,
                        stored_message_as_serializable_dict(
                            message,
                            binary_buffers=binary_buffers,
                        ),
                    )
                )
        return serializer.serialize()

    def serialize_embed(
        self, *, start_timestep: int = 0, end_timestep: int | None = None
    ) -> bytes:
        """Build the standalone viewer recording for the requested timestep range."""
        start, end = self._validate_timesteps(
            start_timestep=start_timestep,
            end_timestep=end_timestep,
        )
        recording: list[tuple[float, StoredMessage]] = [
            (0.0, store_raw_message(impl.run_javascript_message(runtime_source())))
        ]
        recording.extend(
            (0.0, store_raw_message(message)) for message in self._static_messages()
        )

        fps = self._server.fps
        for step in range(end + 1):
            time = 0.0 if step <= start else (step - start) / fps
            step_state = self._timeline.step(step)
            recording.extend(
                (time, message) for message in step_state.scene_updates.values()
            )
            recording.extend(
                (
                    time,
                    StoredMessage(
                        {**message.payload, "__viserPlaybackTime": time},
                        message.buffers,
                    ),
                )
                for message in step_state.audio_updates
            )
        return serialize_viser_embed_recording(
            recording,
            duration_seconds=max(end - start, 0) / fps,
        )

    def _validate_timesteps(
        self,
        *,
        start_timestep: int,
        end_timestep: int | None,
    ) -> tuple[int, int]:
        start = start_timestep
        end = end_timestep if end_timestep is not None else self._server.num_steps - 1
        if not 0 <= start < self._server.num_steps:
            raise ValueError(
                f"start_timestep must be in [0, {self._server.num_steps - 1}], "
                f"got {start}."
            )
        if not 0 <= end < self._server.num_steps:
            raise ValueError(
                f"end_timestep must be in [0, {self._server.num_steps - 1}], got {end}."
            )
        if start > end:
            raise ValueError(
                "start_timestep must be less than or equal to end_timestep, "
                f"got {start} > {end}."
            )
        return start, end

    def _create_native_serializer(self) -> viser.infra.StateSerializer:
        owner = SimpleNamespace(_record_handles=[])
        serializer = viser.infra.StateSerializer(
            owner,
            filter=lambda message: "Gui" not in type(message).__name__,
        )
        owner._record_handles.append(serializer)
        return serializer

    def _static_messages(self) -> list[impl.Message]:
        static_messages: list[impl.Message] = []
        for message in impl.broadcast_messages(self._server):
            source = getattr(message, "source", None)
            if isinstance(source, str) and source.startswith(RUNTIME_MARKER):
                continue
            static_messages.append(message)
        return static_messages
