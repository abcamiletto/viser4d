from __future__ import annotations

import contextlib
from functools import partial
from typing import TYPE_CHECKING, Iterator

import numpy as np
from viser import _messages

from .. import _viser_private as impl
from ..audio._api import AudioHandle, AudioState, audio_array_payload
from ..audio._messages import AddAudioMessage
from ._store import (
    TimelineRecorder,
    TimelineStore,
    is_scene_message,
    serialize_message,
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
        timeline_names = set(self._timeline.node_names)
        validate_message = partial(_validate_msg, timeline_names=timeline_names)
        recorder.register_callback(validate_message)

        self._active_step = step
        try:
            with impl.scene_recording_interface(self._server.scene, recorder):
                yield
        finally:
            self._active_step = None

        if not recorder.messages:
            return

        step_store = self._timeline.record_messages(step, recorder.messages)
        for node_name in step_store.node_names:
            self._register_timeline_node(node_name)

        # Preload step data into connected runtimes so playback stays in sync live.
        messages = [serialize_message(message) for message in recorder.messages]
        self._server._send_runtime_call(
            "preloadStep",
            {
                "step": step,
                "messages": messages,
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
        self._timeline.record_messages(self._active_step, [message])
        self._server._send_runtime_call(
            "preloadStep",
            {"step": self._active_step, "messages": [serialize_message(message)]},
        )
        return handle

    def dispatch_audio_update(self, message: _messages.Message) -> None:
        """Route audio updates either into the active step or directly to clients."""
        if self._active_step is not None:
            self._timeline.record_messages(self._active_step, [message])
            self._server._send_runtime_call(
                "preloadStep",
                {
                    "step": self._active_step,
                    "messages": [serialize_message(message)],
                },
            )
            return
        self._server._send_runtime_call(
            "applyMessageUpdate", serialize_message(message)
        )

    def _register_timeline_node(self, name: str) -> None:
        if name in self._timeline.baseline_messages_by_name:
            return
        baseline = self._collect_live_messages_for_name(name)
        if not baseline:
            return
        # Baseline messages rebuild the node before step-local diffs are replayed.
        self._timeline.baseline_messages_by_name[name] = baseline
        messages = [serialize_message(message) for message in baseline]
        self._server._send_runtime_call(
            "setBaseline",
            {"name": name, "messages": messages},
        )

    def _collect_live_messages_for_name(self, name: str) -> list[_messages.Message]:
        return [
            message
            for message in impl.broadcast_messages(self._server)
            if is_scene_message(message) and getattr(message, "name", None) == name
        ]


def _validate_msg(
    message: _messages.Message,
    timeline_names: set[str],
) -> None:
    """Make sure that the message only modifies nodes that are part of the timeline."""
    if not is_scene_message(message):
        return
    name = getattr(message, "name", None)
    if not isinstance(name, str) or not name:
        return
    # `viser` uses one family of message classes for node creation and another
    # for later transform/property updates, so we distinguish them explicitly.
    if isinstance(message, _messages._CreateSceneNodeMessage):
        timeline_names.add(name)
        return
    if name in timeline_names:
        return
    raise RuntimeError(f"Cannot modify static scene node {name!r} inside server.at(t).")
