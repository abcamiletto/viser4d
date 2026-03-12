from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Iterator

import numpy as np
from viser import _messages

from .. import _viser_private as impl
from ..audio._api import AudioHandle, AudioState, audio_array_payload
from ..audio._messages import AddAudioMessage
from .._types import StoredMessage
from ._store import (
    TimelineRecorder,
    TimelineStep,
    TimelineStore,
    extract_message_name,
    is_create_scene_message,
    is_scene_message,
    serialize_stored_message,
    serialize_stored_messages,
    store_raw_message,
    store_raw_messages,
)

if TYPE_CHECKING:
    from .._server import Viser4dServer


class SceneRecorder:
    """Capture per-timestep scene and audio edits from the live viser API."""

    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._timeline = timeline
        self._active_step: int | None = None

    @property
    def active_step(self) -> int | None:
        return self._active_step

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[None]:
        """Record scene changes performed inside the context for timestep ``t``."""
        step = self._timeline.validate_step(t)
        recorder = TimelineRecorder()

        # Static scene nodes should stay outside the timeline model.
        timeline_names = set(self._timeline.iter_node_names())

        self._active_step = step
        try:
            with impl.scene_recording_interface(self._server.scene, recorder):
                yield
        finally:
            self._active_step = None

        if not recorder.messages:
            return

        stored_messages = store_raw_messages(recorder.messages)
        _validate_messages(stored_messages, timeline_names)
        step_store = self._record_and_preload(step, stored_messages)
        for node_name in step_store.node_names:
            self._register_timeline_node(node_name)

    def add_audio(
        self,
        name: str,
        *,
        data: np.ndarray,
        sample_rate: int,
    ) -> AudioHandle:
        """Create a timeline-owned audio track for the active timestep."""
        state = AudioState(
            name=name,
            sample_rate=sample_rate,
            waveform=np.ascontiguousarray(data),
        )
        handle = AudioHandle(self._server, state)
        assert self._active_step is not None
        message = AddAudioMessage(
            name=name,
            sampleRate=state.sample_rate,
            waveform=audio_array_payload(state.waveform),
            volume=state.volume,
        )
        stored_messages = store_raw_messages([message])
        self._record_and_preload(self._active_step, stored_messages)
        return handle

    def dispatch_audio_update(self, message: _messages.Message) -> None:
        """Route audio updates either into the active step or directly to clients."""
        if self._active_step is not None:
            stored_messages = store_raw_messages([message])
            self._record_and_preload(self._active_step, stored_messages)
            return
        stored_message = store_raw_message(message)
        self._server._send_runtime_call(
            "applyMessageUpdate", serialize_stored_message(stored_message)
        )

    def _register_timeline_node(self, name: str) -> None:
        if self._timeline.has_saved_baseline(name):
            return
        baseline = self._collect_live_messages_for_name(name)
        if not baseline:
            return
        # Baseline messages rebuild the node before step-local diffs are replayed.
        self._timeline.set_baseline(name, baseline)
        messages = serialize_stored_messages(baseline)
        self._server._send_runtime_call(
            "setBaseline",
            {"name": name, "messages": messages},
        )

    def _record_and_preload(
        self, step: int, stored_messages: list[StoredMessage]
    ) -> TimelineStep:
        """Store one timestep and preload its serialized updates into live runtimes."""
        step_state = self._timeline.record_step(step, stored_messages)
        payload = {
            "step": step,
            "messages": serialize_stored_messages(stored_messages),
            "nodeNames": sorted(step_state.node_names),
        }
        self._server._send_runtime_call("preloadStep", payload)
        return step_state

    def _collect_live_messages_for_name(self, name: str) -> list[StoredMessage]:
        return [
            message
            for message in store_raw_messages(impl.broadcast_messages(self._server))
            if is_scene_message(message) and message.get("name") == name
        ]


def _validate_messages(
    stored_messages: list[StoredMessage],
    timeline_names: set[str],
) -> None:
    """Make sure that recorded messages only modify timeline-owned nodes."""
    for stored_message in stored_messages:
        if not is_scene_message(stored_message):
            continue
        name = extract_message_name(stored_message)
        if name is None:
            continue
        if is_create_scene_message(stored_message):
            timeline_names.add(name)
            continue
        if name in timeline_names:
            continue
        raise RuntimeError(
            f"Cannot modify static scene node {name!r} inside server.at(t)."
        )
