"""Export the recorded timeline via viser's native ``StateSerializer``.

We seed the serializer from the live broadcast buffer (which carries the injected
runtime JS, so exported HTML gets audio sync for free), then walk steps: fold a
running scene state, append each step's materialized delta plus the overrides for
nodes that exist at that step, with ``insert_sleep`` between steps. Steps before
``start_timestep`` land at time 0; their audio events are folded instead of
emitted, then re-synthesized at the start step with the elapsed portion trimmed.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from rich.progress import track

from . import _state, _viser
from ._protocol import AddAudioMessage, ScenePayload
from ._state import AudioState, SceneEntryRecord, SceneState, StoredMessage

if TYPE_CHECKING:
    import viser.infra

    from ._server import Viser4dServer


class ExportBuilder:
    def __init__(self, server: Viser4dServer) -> None:
        self._server = server

    def serialize(
        self, *, start_timestep: int = 0, end_timestep: int | None = None
    ) -> bytes:
        return self._build(start_timestep, end_timestep).serialize()

    def as_html(
        self,
        *,
        dark_mode: bool = False,
        start_timestep: int = 0,
        end_timestep: int | None = None,
    ) -> str:
        return self._build(start_timestep, end_timestep).as_html(dark_mode=dark_mode)

    def _validate(self, start: int, end: int | None) -> tuple[int, int]:
        last = self._server.num_steps - 1
        stop = last if end is None else end
        if not 0 <= start <= last:
            raise ValueError(f"start_timestep must be in [0, {last}], got {start}.")
        if not 0 <= stop <= last:
            raise ValueError(f"end_timestep must be in [0, {last}], got {stop}.")
        if start > stop:
            raise ValueError(
                "start_timestep must be less than or equal to end_timestep, "
                f"got {start} > {stop}."
            )
        return start, stop

    def _build(self, start: int, end: int | None) -> viser.infra.StateSerializer:
        start, stop = self._validate(start, end)
        timeline = self._server._timeline
        serializer = self._server.get_scene_serializer()
        binary_buffers = _viser.serializer_binary_buffers(serializer)
        overrides = timeline.override_items()
        remapped_overrides: dict[tuple[str, int], ScenePayload] = {}
        state = SceneState()
        audio = AudioState()
        for step in track(
            range(stop + 1),
            description="Exporting .viser",
            disable=not sys.stdout.isatty(),
            transient=True,
        ):
            if step > start:
                serializer.insert_sleep(1.0 / self._server.fps)
            delta = timeline.step_delta(step)
            state.apply_delta(delta)
            if step < start:
                # Pre-roll: fold audio instead of emitting mid-clip events.
                for event in delta.audio:
                    audio.apply(event, step)
                messages = _state.materialize(
                    delta.puts.values(), delta.delete_nodes, []
                )
            else:
                messages = _state.materialize_delta(delta)
                if step == start and start > 0:
                    messages = _preroll_audio(audio, start, self._server.fps) + messages
            for message in messages:
                _viser.append_serializer_message(
                    serializer, message.remap(binary_buffers)
                )
            for entry in _visible_overrides(overrides, state):
                payload = remapped_overrides.get((entry.key, entry.rev))
                if payload is None:
                    payload = entry.message.remap(binary_buffers)
                    remapped_overrides[(entry.key, entry.rev)] = payload
                _viser.append_serializer_message(serializer, payload)
        return serializer


def _preroll_audio(audio: AudioState, start: int, fps: float) -> list[StoredMessage]:
    """One AddAudio per live track, trimmed by the portion elapsed before start."""
    out: list[StoredMessage] = []
    for name, snapshot in sorted(audio.tracks.items()):
        skip = round((start - snapshot.start_step) / fps * snapshot.sample_rate)
        frames = len(snapshot.data) // snapshot.num_channels
        if skip >= frames:
            continue
        message = AddAudioMessage(
            name=name,
            sampleRate=snapshot.sample_rate,
            waveform={
                "numChannels": snapshot.num_channels,
                "numFrames": frames - skip,
                "data": snapshot.data[skip * snapshot.num_channels :],
            },
            volume=snapshot.volume,
        )
        out.append(StoredMessage.capture(message))
    return out


def _visible_overrides(
    overrides: list[SceneEntryRecord], state: SceneState
) -> list[SceneEntryRecord]:
    existing = state.node_names()
    out: list[SceneEntryRecord] = []
    for entry in overrides:
        if _state.is_delete_key(entry.key):
            name = entry.name or ""
            if any(_state._covers(name, node) for node in existing):
                out.append(entry)
        elif entry.name is None or entry.name in existing:
            out.append(entry)
    return out
