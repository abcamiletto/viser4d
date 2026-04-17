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


# ---------------------------------------------------------------------------
# Canonical state keys
# ---------------------------------------------------------------------------


def _scene_node_prefix(name: str) -> str:
    return f"scene.node:{name}" if name else "scene.root:"


def scene_message_state_key(message: StoredMessage) -> str | None:
    """Return the canonical viser4d-owned key for one scene message.

    Returns ``None`` for ``RemoveSceneNodeMessage`` because deletes are
    represented separately from keyed puts.
    """
    message_type = message.payload.get("type")
    if not isinstance(message_type, str) or message_type == "RemoveSceneNodeMessage":
        return None

    name_field = message.payload.get("name")
    prefix = (
        _scene_node_prefix(name_field)
        if isinstance(name_field, str)
        else "scene.global"
    )

    if "props" in message.payload:
        return f"{prefix}:create"

    if message_type == "SceneNodeUpdateMessage":
        return f"{prefix}:update"
    if message_type == "SetOrientationMessage":
        return f"{prefix}:prop:orientation"
    if message_type == "SetPositionMessage":
        return f"{prefix}:prop:position"
    if message_type == "SetSceneNodeVisibilityMessage":
        return f"{prefix}:prop:visible"
    if message_type == "SetSceneNodeClickableMessage":
        return f"{prefix}:prop:clickable"
    if message_type == "SetBoneOrientationMessage":
        bone_index = message.payload.get("bone_index")
        return f"{prefix}:bone:{bone_index}:orientation"
    if message_type == "SetBonePositionMessage":
        bone_index = message.payload.get("bone_index")
        return f"{prefix}:bone:{bone_index}:position"

    # Generic fallback based on non-standard payload fields.
    payload_keys = tuple(
        str(key) for key in message.payload if key not in {"type", "name", "props"}
    )
    if len(payload_keys) == 1:
        return f"{prefix}:prop:{payload_keys[0]}"
    return f"{prefix}:message:{message_type}"


def scene_delete_state_key(node_name: str) -> str:
    """Canonical key for a ``RemoveSceneNodeMessage`` override."""
    return f"scene.node:{node_name}:delete"


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
                    "key": f"{_scene_node_prefix(name)}:prop:{prop}",
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

    key = scene_message_state_key(message)
    return ([{"key": key, "message": message}], []) if key is not None else ([], [])


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
