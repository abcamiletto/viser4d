from __future__ import annotations

from typing import TYPE_CHECKING

from . import _viser_private as impl
from ._types import StoredMessage
from ._runtime import RUNTIME_MARKER, runtime_source
from .timeline._messages_util import (
    serialize_viser_recording,
    store_raw_message,
    store_raw_messages,
)
from .timeline._store import TimelineStore

if TYPE_CHECKING:
    from ._server import Viser4dServer


class ExportBuilder:
    """Serialize the current timeline into viser's recording format."""

    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._timeline = timeline

    def serialize(
        self, *, start_timestep: int = 0, end_timestep: int | None = None
    ) -> bytes:
        """Build a `.viser` recording for the requested timestep range."""
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

        # Bootstrap playback with the injected runtime before any timeline messages arrive.
        runtime_source_message = impl.run_javascript_message(runtime_source())
        runtime_message = store_raw_message(runtime_source_message)
        recording: list[tuple[float, StoredMessage]] = [(0.0, runtime_message)]
        for message in store_raw_messages(impl.broadcast_messages(self._server)):
            source = message.get("source")
            if isinstance(source, str) and source.startswith(RUNTIME_MARKER):
                continue
            name = message.get("name")
            if isinstance(name, str) and self._timeline.has_node(name):
                continue
            recording.append((0.0, message))
        fps = self._server.fps
        for step in range(end + 1):
            time = 0.0 if step <= start else (step - start) / fps
            step_state = self._timeline.step(step)
            recording.extend(
                (time, message) for message in step_state.scene_updates.values()
            )
            recording.extend(
                (time, {**message, "__viserPlaybackTime": time})
                for message in step_state.audio_updates
            )

        blob = serialize_viser_recording(
            recording,
            duration_seconds=max(end - start, 0) / fps,
        )
        return blob
