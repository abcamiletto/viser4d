"""The canonical keyed scene/audio model: capture, keys, fold, wire.

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

The browser materializes parent-before-child scene updates from these entries.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple, cast

import msgspec
import numpy as np
from viser_audio import messages as audio_messages

from . import _viser
from ._protocol import (
    AudioPayload,
    Payload,
    SceneEntry,
    ScenePayload,
    TimelineOverrideMessage,
)
from ._protocol import (
    StepDelta as StepDeltaWire,
)

__all__ = ["SceneState", "StepDelta"]

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
        if isinstance(message, audio_messages.AudioMessage):
            payload = message.as_payload(binary_buffers=buffers)
        else:
            payload = message.as_serializable_dict(binary_buffers=buffers)
        return cls(payload, tuple(bytes(b) for b in buffers))

    def inflate(self) -> ScenePayload:
        """Placeholders -> numpy arrays, for sending over the wire."""
        return cast(ScenePayload, _inflate(self.payload, self.buffers))

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
    audio: list[StoredMessage] = []

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
                message=StoredMessage(
                    {"type": "RemoveSceneNodeMessage", "name": name, "owner": ""}
                ),
            )
            put_entry(self.entries, record)
            changed.append(record)
        for put in puts:
            record = SceneEntryRecord(put.key, next_rev(), put.name, put.message)
            put_entry(self.entries, record)
            changed.append(record)
        return changed


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


def delta_to_wire(delta: StepDelta) -> StepDeltaWire:
    return {
        "puts": [entry_to_wire(e) for e in delta.puts.values()],
        "deleteNodes": list(delta.delete_nodes),
        "audio": [AudioPayload(event.inflate()) for event in delta.audio],
    }


def override_message(entry: SceneEntryRecord) -> TimelineOverrideMessage:
    return TimelineOverrideMessage(entry=entry_to_wire(entry))


# ---------------------------------------------------------------------------
# Binary placeholder helpers
# ---------------------------------------------------------------------------


def _inflate(value: Any, buffers: tuple[bytes, ...]) -> Any:
    if isinstance(value, dict):
        idx, dtype = value.get(_BINARY_INDEX), value.get(_DTYPE)
        if isinstance(idx, int) and isinstance(dtype, str):
            return np.frombuffer(buffers[idx], dtype=np.dtype(dtype))
        return {str(k): _inflate(v, buffers) for k, v in value.items()}
    if isinstance(value, list):
        return [_inflate(v, buffers) for v in value]
    return value
