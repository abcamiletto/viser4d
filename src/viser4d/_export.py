"""Export one offline timeline, driven by the same player as live sessions.

The native viser recording holds the static scene and a single timeline command.
Its duration is zero, so only the viser4d playback bar owns time. No audio clock
is inferred from native player controls.
"""

from __future__ import annotations

import base64
import dataclasses
import json
from typing import Any

import numpy as np
import viser.infra
from viser_audio import AudioState
from viser_audio import messages as audio_messages

from . import _state, _viser
from ._state import SceneState
from ._timeline import Timeline


def build(
    serializer: viser.infra.StateSerializer,
    timeline: Timeline,
    fps: float,
    start: int,
    end: int | None,
) -> viser.infra.StateSerializer:
    """Append an offline timeline for the inclusive range [start, end]."""
    start, stop = _validate(timeline, start, end)
    scene = SceneState()
    audio = AudioState()
    for step in range(start):
        delta = timeline.step_delta(step)
        scene.apply_delta(delta)
        for event in delta.audio:
            audio.apply(audio_messages.from_payload(event.inflate()))

    # Preserve complete samples so later replacement and append events still
    # address the original track. Negative anchors account for elapsed pre-roll.
    time_origin = start / fps
    tracks = []
    for track in audio.snapshot():
        assert track.start_time is not None
        shifted = dataclasses.replace(track, start_time=track.start_time - time_origin)
        tracks.append(shifted.as_payload())
    deltas = [
        _state.delta_to_wire(timeline.step_delta(step))
        for step in range(start, stop + 1)
    ]
    for delta in deltas:
        for event in delta["audio"]:
            if event["type"] == "AudioAddMessage":
                event["start_time"] -= time_origin
    recording = {
        "numSteps": stop - start + 1,
        "fps": fps,
        "block": {
            "type": "TimelineBlockMessage",
            "index": 0,
            "checkpointScene": [
                _state.entry_to_wire(entry) for entry in scene.entries.values()
            ],
            "checkpointAudio": tracks,
            "deltas": deltas,
        },
        "overrides": [
            _state.entry_to_wire(entry) for entry in timeline.override_items()
        ],
    }
    payload = json.dumps(recording, default=_encode_array, allow_nan=False)
    source = f"window.__VISER4D__.loadRecording({json.dumps(payload)});"
    command = _viser.run_javascript_message(source).as_serializable_dict()
    _viser.append_serializer_message(serializer, command)
    return serializer


def _validate(timeline: Timeline, start: int, end: int | None) -> tuple[int, int]:
    last = timeline.num_steps - 1
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


def _encode_array(value: Any) -> dict[str, str]:
    # Audio payloads contain memoryviews; scene payloads contain numpy arrays.
    if not isinstance(value, (np.ndarray, memoryview)):
        raise TypeError(f"Cannot encode timeline value: {type(value).__name__}")
    array = np.asarray(value)
    dtype = array.dtype.newbyteorder("<")
    data = np.ascontiguousarray(array, dtype=dtype)
    return {
        "__typed_array": dtype.str,
        "base64": base64.b64encode(data.data).decode("ascii"),
    }
