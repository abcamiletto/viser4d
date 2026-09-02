"""The canonical keyed scene/audio model: capture, keys, fold, materialize, wire.

Every scene-mutating viser message reduces to keyed *puts* or *node deletes*, so
the scene at any timestep is a map ``key -> SceneEntryRecord``. Clients receive
``name`` explicitly and never parse keys.

Key derivation:

| message shape                 | key                                        |
|-------------------------------|--------------------------------------------|
| has ``props`` (node creation) | ``create:{name}``                          |
| ``SceneNodeUpdateMessage``    | one put per prop, ``update:{name}:{prop}`` |
| other message with a name     | ``{type}:{name}`` (+ ``:{bone_index}``)    |
| message without a name        | ``{type}`` (global state)                  |

Fold rules live in ``put_entry`` / ``delete_node_entries`` and nowhere else:

- delete node ``n``: drop every entry whose node is ``n`` or a descendant.
- put a create for ``n``: first drop ``n``'s own non-create entries (re-creating
  a node resets its properties, descendants survive, which matches viser's
  client-side upsert), then store the entry.
- any other put: last write wins per key.

A node exists iff its ``create:{name}`` key is present. Every entry carries a
globally monotonic ``rev``; two entries are equal iff their revs are equal.

Materialize ordering: node removals (topmost ancestors only), then global
(nameless) entries, then nodes parent-before-child, each node's create first.
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from collections.abc import Callable, Iterable
from typing import Any, NamedTuple, cast

import msgspec
import numpy as np

from . import _viser
from ._protocol import (
    AUDIO_MESSAGE_TYPES,
    AudioPayload,
    AudioTrack,
    Payload,
    SceneEntry,
    ScenePayload,
    TimelineOverrideMessage,
    Waveform,
)
from ._protocol import (
    StepDelta as StepDeltaWire,
)

__all__ = ["AudioState", "SceneState", "StepDelta"]

_BINARY_INDEX = "__binary_index"
_DTYPE = "dtype"
_CREATE_PREFIX = "create:"
_DELETE_PREFIX = "RemoveSceneNodeMessage:"


# ---------------------------------------------------------------------------
# Stored messages: placeholder payload + detached binary buffers
# ---------------------------------------------------------------------------


class StoredMessage(msgspec.Struct, frozen=True):
    """One viser message captured as a placeholder payload plus its buffers."""

    payload: Payload
    buffers: tuple[bytes, ...] = ()

    @classmethod
    def capture(cls, message: _viser.Message) -> StoredMessage:
        buffers: list[memoryview] = []
        payload = message.as_serializable_dict(binary_buffers=buffers)
        return cls(payload, tuple(bytes(b) for b in buffers))

    def inflate(self) -> ScenePayload:
        """Placeholders -> numpy arrays, for sending over the wire."""
        return cast(ScenePayload, _inflate(self.payload, self.buffers))

    def remap(self, binary_buffers: list[memoryview]) -> ScenePayload:
        """Placeholders -> offset placeholders into a serializer's buffer list."""
        offset = len(binary_buffers)
        binary_buffers.extend(_byte_view(b) for b in self.buffers)
        return cast(ScenePayload, _remap(self.payload, offset, len(self.buffers)))

    @property
    def type(self) -> str:
        return self.payload["type"]

    @property
    def name(self) -> str | None:
        return self.payload.get("name")


class SceneEntryRecord(msgspec.Struct, frozen=True):
    """One keyed put, stamped with the rev it was recorded at."""

    key: str
    rev: int
    name: str | None
    message: StoredMessage


class AudioEventRecord(msgspec.Struct, frozen=True):
    rev: int
    message: StoredMessage


class Put(NamedTuple):
    """One keyed put derived from a message, before a rev is stamped."""

    key: str
    name: str | None
    message: StoredMessage


# ---------------------------------------------------------------------------
# Keys and fold rules
# ---------------------------------------------------------------------------


def is_create_key(key: str) -> bool:
    return key.startswith(_CREATE_PREFIX)


def is_delete_key(key: str) -> bool:
    return key.startswith(_DELETE_PREFIX)


def is_audio(stored: StoredMessage) -> bool:
    return stored.type in AUDIO_MESSAGE_TYPES


def covers(root: str, node: str) -> bool:
    """True if ``node`` is ``root`` or a descendant of it."""
    return node == root or node.startswith(f"{root}/")


def scene_puts_deletes(stored: StoredMessage) -> tuple[list[Put], list[str]]:
    """Reduce one scene message to keyed puts and node-delete names."""
    mtype = stored.type
    if mtype == "RemoveSceneNodeMessage":
        return ([], [stored.name]) if stored.name else ([], [])
    if mtype == "SceneNodeUpdateMessage":
        name = stored.name
        if name is None:
            return [], []
        updates = cast(dict[str, Any], stored.payload.get("updates", {}))
        return [
            Put(
                f"update:{name}:{prop}",
                name,
                StoredMessage(
                    {**stored.payload, "updates": {prop: value}}, stored.buffers
                ),
            )
            for prop, value in updates.items()
        ], []
    if "props" in stored.payload:
        return [Put(f"{_CREATE_PREFIX}{stored.name}", stored.name, stored)], []
    if stored.name is not None:
        key = f"{mtype}:{stored.name}"
        if "bone_index" in stored.payload:
            key = f"{key}:{stored.payload['bone_index']}"
        return [Put(key, stored.name, stored)], []
    return [Put(mtype, None, stored)], []


def put_entry(entries: dict[str, SceneEntryRecord], entry: SceneEntryRecord) -> None:
    """Store one keyed put, resetting the node's own props on re-creation."""
    if is_create_key(entry.key):
        for key in [
            k
            for k, e in entries.items()
            if e.name == entry.name and not is_create_key(k)
        ]:
            del entries[key]
    entries.pop(entry.key, None)
    entries[entry.key] = entry


def delete_node_entries(entries: dict[str, SceneEntryRecord], name: str) -> None:
    """Drop every entry whose node is ``name`` or a descendant of it."""
    for key in [k for k, e in entries.items() if e.name and covers(name, e.name)]:
        del entries[key]


# ---------------------------------------------------------------------------
# Step delta: a scene state fragment plus the nodes the step deletes
# ---------------------------------------------------------------------------


class StepDelta(msgspec.Struct):
    """Everything one recorded timestep applies on top of the previous state."""

    puts: dict[str, SceneEntryRecord] = {}
    delete_nodes: list[str] = []
    audio: list[AudioEventRecord] = []

    def is_empty(self) -> bool:
        return not self.puts and not self.delete_nodes and not self.audio

    def fold_delete(self, name: str) -> None:
        if any(covers(existing, name) for existing in self.delete_nodes):
            return
        self.delete_nodes = [d for d in self.delete_nodes if not covers(name, d)]
        self.delete_nodes.append(name)
        delete_node_entries(self.puts, name)

    def fold_put(self, entry: SceneEntryRecord) -> None:
        put_entry(self.puts, entry)


# ---------------------------------------------------------------------------
# Folded scene state
# ---------------------------------------------------------------------------


class SceneState:
    def __init__(self) -> None:
        self.entries: dict[str, SceneEntryRecord] = {}

    def copy(self) -> SceneState:
        clone = SceneState()
        clone.entries = dict(self.entries)
        return clone

    def node_names(self) -> set[str]:
        return {
            e.name for e in self.entries.values() if is_create_key(e.key) and e.name
        }

    def delete_node(self, name: str) -> None:
        delete_node_entries(self.entries, name)

    def put(self, entry: SceneEntryRecord) -> None:
        put_entry(self.entries, entry)

    def apply_delta(self, delta: StepDelta) -> None:
        for name in delta.delete_nodes:
            self.delete_node(name)
        for entry in delta.puts.values():
            self.put(entry)


# ---------------------------------------------------------------------------
# Override overlay (writes made outside server.at(t))
# ---------------------------------------------------------------------------


class OverrideState:
    """Keyed overlay applied on top of every step.

    An override for node ``n`` applies wherever ``n`` exists. Deletes are kept
    as tombstone entries so the node stays deleted at every step; a tombstone
    also prunes the overlay entries it covers.
    """

    def __init__(self) -> None:
        self.entries: dict[str, SceneEntryRecord] = {}

    def clear(self) -> None:
        self.entries.clear()

    def items(self) -> list[SceneEntryRecord]:
        return list(self.entries.values())

    def apply(
        self, stored: StoredMessage, next_rev: Callable[[], int]
    ) -> list[SceneEntryRecord]:
        puts, deletes = scene_puts_deletes(stored)
        changed: list[SceneEntryRecord] = []
        for name in deletes:
            if any(
                is_delete_key(k) and e.name and covers(e.name, name)
                for k, e in self.entries.items()
            ):
                continue
            delete_node_entries(self.entries, name)
            record = SceneEntryRecord(
                key=f"{_DELETE_PREFIX}{name}",
                rev=next_rev(),
                name=name,
                message=StoredMessage({"type": "RemoveSceneNodeMessage", "name": name}),
            )
            put_entry(self.entries, record)
            changed.append(record)
        for put in puts:
            record = SceneEntryRecord(put.key, next_rev(), put.name, put.message)
            put_entry(self.entries, record)
            changed.append(record)
        return changed


# ---------------------------------------------------------------------------
# Folded audio state
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class AudioTrackSnapshot:
    rev: int
    sample_rate: int
    start_step: int
    volume: float
    num_channels: int
    data: np.ndarray  # flat float32, frame-major


class AudioState:
    """Audio events folded into per-track snapshots.

    A track occupies steps ``[start_step, start_step + frames / rate * fps)``;
    ``AddAudioMessage`` anchors ``start_step``, later events never move it.
    """

    def __init__(self) -> None:
        self.tracks: dict[str, AudioTrackSnapshot] = {}

    def copy(self) -> AudioState:
        clone = AudioState()
        clone.tracks = {
            name: dataclasses.replace(track, data=track.data)
            for name, track in self.tracks.items()
        }
        return clone

    def apply(self, event: AudioEventRecord, step: int) -> None:
        payload = event.message.inflate()
        mtype = payload.get("type")
        name = payload.get("name")
        if not isinstance(name, str):
            return
        if mtype == "RemoveAudioMessage":
            self.tracks.pop(name, None)
            return
        if mtype == "AddAudioMessage":
            channels, data = _waveform_samples(payload["waveform"])
            self.tracks[name] = AudioTrackSnapshot(
                rev=event.rev,
                sample_rate=int(payload["sampleRate"]),
                start_step=step,
                volume=float(payload["volume"]),
                num_channels=channels,
                data=data,
            )
            return
        track = self.tracks.get(name)
        if track is None:
            return
        track.rev = event.rev
        if mtype == "SetAudioVolumeMessage":
            track.volume = float(payload["volume"])
        elif mtype == "SetAudioWaveformMessage":
            track.num_channels, track.data = _waveform_samples(payload["waveform"])
        elif mtype == "AppendAudioMessage":
            _, tail = _waveform_samples(payload["waveform"])
            track.data = np.concatenate((track.data, tail))


# ---------------------------------------------------------------------------
# Materialize ordering (delta / state -> ordered viser messages)
# ---------------------------------------------------------------------------


def materialize(
    entries: Iterable[SceneEntryRecord],
    delete_nodes: Iterable[str],
    audio_messages: Iterable[StoredMessage],
) -> list[StoredMessage]:
    """Turn a keyed put set + deletes + audio into an ordered message list."""
    out: list[StoredMessage] = []
    for name in _topmost(delete_nodes):
        out.append(StoredMessage({"type": "RemoveSceneNodeMessage", "name": name}))
    entries = list(entries)
    out.extend(e.message for e in entries if e.name is None)
    by_node: defaultdict[str, list[SceneEntryRecord]] = defaultdict(list)
    for entry in entries:
        if entry.name is not None:
            by_node[entry.name].append(entry)
    for name in sorted(by_node, key=lambda n: (n.count("/"), n)):
        node = by_node[name]
        out.extend(e.message for e in node if is_create_key(e.key))
        out.extend(e.message for e in node if not is_create_key(e.key))
    out.extend(audio_messages)
    return out


def materialize_delta(delta: StepDelta) -> list[StoredMessage]:
    return materialize(
        delta.puts.values(),
        delta.delete_nodes,
        [event.message for event in delta.audio],
    )


def _topmost(delete_nodes: Iterable[str]) -> list[str]:
    nodes = list(delete_nodes)
    return [n for n in nodes if not any(o != n and covers(o, n) for o in nodes)]


# ---------------------------------------------------------------------------
# Wire conversion
# ---------------------------------------------------------------------------


def entry_to_wire(entry: SceneEntryRecord) -> SceneEntry:
    return {
        "key": entry.key,
        "rev": entry.rev,
        "name": entry.name,
        "message": entry.message.inflate(),
    }


def audio_event_to_wire(event: AudioEventRecord) -> AudioPayload:
    return AudioPayload(event.message.inflate())


def delta_to_wire(delta: StepDelta) -> StepDeltaWire:
    return {
        "puts": [entry_to_wire(e) for e in delta.puts.values()],
        "deleteNodes": list(delta.delete_nodes),
        "audio": [audio_event_to_wire(a) for a in delta.audio],
    }


def audio_track_to_wire(name: str, track: AudioTrackSnapshot) -> AudioTrack:
    frames = len(track.data) // track.num_channels if track.num_channels else 0
    waveform: Waveform = {
        "numChannels": track.num_channels,
        "numFrames": frames,
        "data": np.ascontiguousarray(track.data, dtype=np.float32),
    }
    return {
        "name": name,
        "sampleRate": track.sample_rate,
        "startStep": track.start_step,
        "volume": track.volume,
        "waveform": waveform,
    }


def override_message(entry: SceneEntryRecord) -> TimelineOverrideMessage:
    return TimelineOverrideMessage(entry=entry_to_wire(entry))


# ---------------------------------------------------------------------------
# Binary placeholder helpers
# ---------------------------------------------------------------------------


def _byte_view(value: bytes) -> memoryview:
    view = memoryview(value)
    return view if view.format == "B" else view.cast("B")


def _inflate(value: Any, buffers: tuple[bytes, ...]) -> Any:
    if isinstance(value, dict):
        idx, dtype = value.get(_BINARY_INDEX), value.get(_DTYPE)
        if isinstance(idx, int) and isinstance(dtype, str):
            return np.frombuffer(buffers[idx], dtype=np.dtype(dtype))
        return {str(k): _inflate(v, buffers) for k, v in value.items()}
    if isinstance(value, list):
        return [_inflate(v, buffers) for v in value]
    return value


def _remap(value: Any, offset: int, count: int) -> Any:
    if isinstance(value, dict):
        idx, dtype = value.get(_BINARY_INDEX), value.get(_DTYPE)
        if isinstance(idx, int) and isinstance(dtype, str):
            if not 0 <= idx < count:
                raise ValueError(f"Binary buffer index {idx} is out of range.")
            return {_BINARY_INDEX: offset + idx, _DTYPE: dtype}
        return {str(k): _remap(v, offset, count) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_remap(v, offset, count) for v in value]
    return value


def _waveform_samples(waveform: Any) -> tuple[int, np.ndarray]:
    data = np.ascontiguousarray(waveform["data"], dtype=np.float32).reshape(-1)
    return int(waveform["numChannels"]), data
