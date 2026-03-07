from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Iterator

import numpy as np
from viser import _messages

from . import _viser_private as impl
from ._audio import AudioHandle, AudioState, audio_array_payload
from ._protocol import AddAudioOp, AudioOp
from ._timeline import (
    TimelineRecorder,
    TimelineStore,
    is_scene_message,
    serialize_message,
)

if TYPE_CHECKING:
    from ._server import Viser4dServer


class SceneRecorder:
    def __init__(self, server: Viser4dServer, timeline: TimelineStore) -> None:
        self._server = server
        self._timeline = timeline
        self._active_step: int | None = None

    @property
    def active_step(self) -> int | None:
        return self._active_step

    @contextlib.contextmanager
    def at(self, t: int) -> Iterator[None]:
        step = self._timeline.validate_step(int(t))
        recorder = TimelineRecorder()
        self._active_step = step
        try:
            with impl.scene_recording_interface(self._server.scene, recorder):
                yield
        finally:
            self._active_step = None

        if not recorder.messages:
            return

        step_store = self._timeline.record_scene_messages(step, recorder.messages)
        for node_name in step_store.node_names:
            self._register_timeline_node(node_name)

        self._server._send_runtime_call(
            "preloadSceneStep",
            {
                "step": step,
                "messages": [serialize_message(message) for message in recorder.messages],
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
        state = AudioState(
            name=name,
            sample_rate=int(sample_rate),
            waveform=np.ascontiguousarray(data),
        )
        handle = AudioHandle(self._server, state)
        assert self._active_step is not None
        op = AddAudioOp(
            op="add",
            name=name,
            sampleRate=state.sample_rate,
            waveform=audio_array_payload(state.waveform),
            volume=state.volume,
        )
        self._timeline.record_audio_ops(self._active_step, [op])
        self._server._send_runtime_call(
            "preloadAudioStep",
            {"step": self._active_step, "ops": [op]},
        )
        return handle

    def dispatch_audio_update(self, op: AudioOp) -> None:
        if self._active_step is not None:
            self._timeline.record_audio_ops(self._active_step, [op])
            self._server._send_runtime_call(
                "preloadAudioStep",
                {"step": self._active_step, "ops": [op]},
            )
            return
        self._server._send_runtime_call("applyAudioUpdate", op)

    def _register_timeline_node(self, name: str) -> None:
        if name in self._timeline.baseline_messages_by_name:
            return
        baseline = self._collect_live_messages_for_name(name)
        if not baseline:
            return
        self._timeline.baseline_messages_by_name[name] = baseline
        self._server._send_runtime_call(
            "setBaseline",
            {"name": name, "messages": [serialize_message(message) for message in baseline]},
        )

    def _collect_live_messages_for_name(self, name: str) -> list[_messages.Message]:
        return [
            message
            for message in impl.broadcast_messages(self._server)
            if is_scene_message(message) and getattr(message, "name", None) == name
        ]
