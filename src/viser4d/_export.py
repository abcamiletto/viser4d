from __future__ import annotations

from typing import TYPE_CHECKING

from viser import _messages

from . import _viser_private as impl
from .audio._messages import is_audio_message_type
from ._types import StoredMessage
from ._runtime import RUNTIME_MARKER, runtime_source
from .timeline import (
    TimelineStore,
    serialize_viser_recording,
    store_raw_message,
    store_raw_messages,
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
        runtime_source_message = _messages.RunJavascriptMessage(runtime_source())
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
        # Timeline-managed nodes are reconstructed from their saved baseline plus step diffs.
        for baseline in self._timeline.iter_baselines():
            recording.extend((0.0, message) for message in baseline)
        fps = max(self._server._base_fps, 1.0)
        for step in range(end + 1):
            time = 0.0 if step <= start else (step - start) / fps
            step_state = self._timeline.step(step)
            recording.extend(
                (time, update.message) for update in step_state.scene_updates
            )
            recording.extend(
                (time, _with_playback_time(update.message, playback_time=time))
                for update in step_state.audio_updates
            )

        blob = serialize_viser_recording(
            recording,
            duration_seconds=max(end - start, 0) / fps,
        )
        return blob


def _with_playback_time(message: StoredMessage, *, playback_time: float) -> StoredMessage:
    if not is_audio_message_type(message.get("type")):
        return message
    return {**message, "__viserPlaybackTime": playback_time}
