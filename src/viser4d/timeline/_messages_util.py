"""Pure message conversion and canonical state-key utilities for timeline storage."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

from .. import _viser_private as impl
from .._types import StoredMessage, StoredMessageEntry, StoredPayload, StoredStatePatch
from ..audio._messages import is_audio_message_type


@dataclass
class TimelineStep:
    """Canonical per-step patch against the prior timeline state."""

    scene_puts: dict[str, StoredMessage] = field(default_factory=dict)
    scene_delete_nodes: list[str] = field(default_factory=list)
    audio_messages: list[StoredMessage] = field(default_factory=list)


def _is_same_node_or_descendant(name: str, root: str) -> bool:
    return name == root or name.startswith(f"{root}/")


def store_raw_message(message: impl.Message) -> StoredMessage:
    """Capture one viser message in placeholder-plus-buffer form."""
    buffers: list[memoryview] = []
    payload = cast(
        StoredPayload,
        message.as_serializable_dict(binary_buffers=buffers),
    )
    return StoredMessage(payload, tuple(bytes(buffer) for buffer in buffers))


def stored_int(value: object) -> int:
    if not isinstance(value, (int, float)):
        raise TypeError(f"Expected int-like stored value, got {type(value).__name__}.")
    return int(value)


def stored_float(value: object) -> float:
    if not isinstance(value, (int, float)):
        raise TypeError(
            f"Expected float-like stored value, got {type(value).__name__}."
        )
    return float(value)


def stored_dict(value: object) -> StoredPayload:
    if not isinstance(value, dict):
        raise TypeError(f"Expected dict stored value, got {type(value).__name__}.")
    return cast(StoredPayload, value)


def extract_message_name(message: StoredMessage) -> str | None:
    name = message.payload.get("name")
    return name if isinstance(name, str) and name else None


def is_scene_message(message: StoredMessage) -> bool:
    message_type = message.payload.get("type")
    return (
        isinstance(message_type, str)
        and not message_type.startswith("Gui")
        and not is_audio_message_type(message_type)
    )


def scene_delete_state_key(node_name: str) -> str:
    """Canonical key for a ``RemoveSceneNodeMessage`` override."""
    return f"scene:delete:{node_name}"


def scene_entries_for_message(
    message: StoredMessage,
) -> tuple[list[StoredMessageEntry], list[str]]:
    """Convert one raw scene message into canonical keyed puts and deletes."""
    message_type = message.payload.get("type")
    if not isinstance(message_type, str):
        return [], []

    if message_type == "RemoveSceneNodeMessage":
        name = extract_message_name(message)
        return ([], [name]) if name is not None else ([], [])

    if message_type == "SceneNodeUpdateMessage":
        name = extract_message_name(message)
        if name is None:
            return [], []
        updates = stored_dict(message.payload.get("updates", {}))
        entries: list[StoredMessageEntry] = []
        for prop, value in updates.items():
            entries.append(
                {
                    "key": f"scene:update:{name}:{prop}",
                    "message": StoredMessage(
                        payload={
                            **message.payload,
                            "updates": {str(prop): value},
                        },
                        buffers=message.buffers,
                    ),
                }
            )
        return entries, []

    if "props" in message.payload:
        name = message.payload.get("name")
        key = (
            f"scene:create:{name}"
            if isinstance(name, str)
            else f"scene:create:{message_type}"
        )
    else:
        key = f"scene:{_message_identity(message)}"
    return ([{"key": key, "message": message}], [])


def record_scene_delete(step: TimelineStep, node_name: str) -> None:
    """Record one scene-node removal into a step patch."""
    if any(
        _is_same_node_or_descendant(node_name, existing)
        for existing in step.scene_delete_nodes
    ):
        return
    step.scene_delete_nodes = [
        existing
        for existing in step.scene_delete_nodes
        if not _is_same_node_or_descendant(existing, node_name)
    ]
    step.scene_delete_nodes.append(node_name)
    step.scene_puts = {
        key: message
        for key, message in step.scene_puts.items()
        if not _is_same_node_or_descendant(
            extract_message_name(message) or "",
            node_name,
        )
    }


def record_scene_message(step: TimelineStep, message: StoredMessage) -> None:
    """Fold one stored scene message into a step patch."""
    entries, delete_nodes = scene_entries_for_message(message)
    for node_name in delete_nodes:
        record_scene_delete(step, node_name)
    for entry in entries:
        step.scene_puts.pop(entry["key"], None)
        step.scene_puts[entry["key"]] = entry["message"]


def step_patch_payload(step: TimelineStep) -> StoredStatePatch:
    """Serialize one ``TimelineStep`` into a ``StoredStatePatch`` dict."""
    return {
        "scenePuts": [
            {"key": key, "message": message} for key, message in step.scene_puts.items()
        ],
        "sceneDeleteNodes": list(step.scene_delete_nodes),
        "audioMessages": list(step.audio_messages),
    }


def timeline_step_from_patch_payload(patch: StoredStatePatch) -> TimelineStep:
    """Reconstruct a ``TimelineStep`` from a serialised patch dict."""
    return TimelineStep(
        scene_puts={entry["key"]: entry["message"] for entry in patch["scenePuts"]},
        scene_delete_nodes=list(patch["sceneDeleteNodes"]),
        audio_messages=list(patch["audioMessages"]),
    )


def _message_identity(message: StoredMessage) -> str:
    message_type = message.payload.get("type")
    if not isinstance(message_type, str):
        raise TypeError("Stored scene message is missing a string type.")

    parts = [message_type]
    name = message.payload.get("name")
    if isinstance(name, str):
        parts.append(name if name else "@root")
    else:
        parts.append("@global")

    if "bone_index" in message.payload:
        parts.append(f"bone={message.payload['bone_index']}")

    return ":".join(parts)
