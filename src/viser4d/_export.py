from __future__ import annotations

from typing import TYPE_CHECKING

from viser import _messages

from . import _viser_private as impl
from .audio._messages import is_audio_message
from ._types import SerializedMessage
from ._runtime import RUNTIME_MARKER, runtime_source
from .timeline import (
    TimelineStore,
    serialize_message,
    serialize_viser_recording,
)

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
        assert 0 <= start < self._server.num_steps
        assert 0 <= end < self._server.num_steps
        assert start <= end

        # Bootstrap playback with the injected runtime before any timeline messages arrive.
        recording: list[tuple[float, SerializedMessage]] = [
            (
                0.0,
                serialize_message(_messages.RunJavascriptMessage(runtime_source())),
            )
        ]
        for message in impl.broadcast_messages(self._server):
            if getattr(message, "source", "").startswith(RUNTIME_MARKER):
                continue
            if getattr(message, "name", None) in self._timeline.node_names:
                continue
            recording.append((0.0, serialize_message(message)))
        # Timeline-managed nodes are reconstructed from their saved baseline plus step diffs.
        for baseline in self._timeline.baseline_messages_by_name.values():
            recording.extend((0.0, serialize_message(message)) for message in baseline)
        fps = max(self._server._base_fps, 1.0)
        for step in range(end + 1):
            time = 0.0 if step <= start else (step - start) / fps
            recording.extend(
                (time, _serialize_export_message(message, playback_time=time))
                for message in self._timeline.step(step).messages
            )

        blob = serialize_viser_recording(
            recording,
            duration_seconds=max(end - start, 0) / fps,
        )
        return blob


def _serialize_export_message(
    message: _messages.Message, *, playback_time: float
) -> SerializedMessage:
    serialized = serialize_message(message)
    if is_audio_message(message):
        serialized["__viserPlaybackTime"] = playback_time
    return serialized
