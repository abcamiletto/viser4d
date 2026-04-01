from __future__ import annotations

from typing import TYPE_CHECKING

from . import _viser_private as impl
from ._types import StoredMessage
from ._runtime import RUNTIME_MARKER, runtime_source
from .timeline._messages_util import (
    serialize_viser_recording,
    store_raw_message,
)
from .timeline._store import TimelineStore

if TYPE_CHECKING:
    from ._server import Viser4dServer


class _StaticExportState:
    """Mirror the current server-broadcast state needed for export.

    Timeline updates are recorded through a separate transport and never enter
    this snapshot, so static export state does not need timeline-name filtering.
    """

    def __init__(self, server: Viser4dServer) -> None:
        self._messages: dict[str, StoredMessage] = {}
        for message in impl.broadcast_messages(server):
            self._insert_message(message)
        impl.register_record_handle(server, self)

    def snapshot(self) -> list[StoredMessage]:
        return list(self._messages.values())

    def _insert_message(self, message: impl.Message) -> None:
        source = getattr(message, "source", None)
        if isinstance(source, str) and source.startswith(RUNTIME_MARKER):
            return

        removed_name = impl.remove_scene_node_name(message)
        if removed_name is not None:
            self._drop_scene_node(removed_name)
            return

        key = message.redundancy_key()
        self._messages.pop(key, None)
        self._messages[key] = store_raw_message(message)

    def _drop_scene_node(self, node_name: str) -> None:
        prefix = node_name.rstrip("/") + "/"
        for key, message in list(self._messages.items()):
            name = message.payload.get("name")
            if isinstance(name, str) and (name == node_name or name.startswith(prefix)):
                del self._messages[key]


class ExportBuilder:
    """Serialize the current timeline into viser's recording format."""

    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._timeline = timeline
        self._static_state = _StaticExportState(server)

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
        for message in self._static_state.snapshot():
            recording.append((0.0, message))
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

        blob = serialize_viser_recording(
            recording,
            duration_seconds=max(end - start, 0) / fps,
        )
        return blob
