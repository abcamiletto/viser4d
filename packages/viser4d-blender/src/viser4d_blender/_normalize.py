from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median
from typing import Any, Callable, cast

import numpy as np

from ._types import (
    GeometryKeyframe,
    JSONValue,
    NodeManifest,
    RecordingManifest,
    RecordingPayload,
    TransformKeyframe,
    VisibilityKeyframe,
)

RUNTIME_MARKER = "/*__VISER4D_RUNTIME__*/"


SUPPORTED_MESSAGE_TYPES = {
    "RunJavascriptMessage",
    "BackgroundImageMessage",
    "FrameMessage",
    "MeshMessage",
    "PointCloudMessage",
    "LineSegmentsMessage",
    "IcosphereMessage",
    "CylinderMessage",
    "SetGuiPanelLabelMessage",
    "SetPositionMessage",
    "SetOrientationMessage",
    "SetSceneNodeVisibilityMessage",
    "RemoveSceneNodeMessage",
    "SceneNodeUpdateMessage",
}

CREATE_MESSAGE_TYPES = {
    "FrameMessage": "frame",
    "MeshMessage": "mesh",
    "PointCloudMessage": "point_cloud",
    "LineSegmentsMessage": "line_segments",
    "IcosphereMessage": "icosphere",
    "CylinderMessage": "cylinder",
}


class UnsupportedViserMessageError(RuntimeError):
    pass


@dataclass
class _NodeState:
    manifest: NodeManifest
    alive: bool = True
    point_count: int | None = None
    segment_count: int | None = None


@dataclass
class _NormalizerState:
    nodes: dict[str, _NodeState] = field(default_factory=dict)
    ordered_names: list[str] = field(default_factory=list)
    time_to_frame: dict[float, int] = field(default_factory=dict)
    ordered_times: list[float] = field(default_factory=list)

    def frame_for(self, time_value: float) -> int:
        frame = self.time_to_frame.get(time_value)
        if frame is not None:
            return frame
        frame = len(self.ordered_times) + 1
        self.time_to_frame[time_value] = frame
        self.ordered_times.append(time_value)
        return frame

    def require_node(
        self, name: str, *, message_type: str, time_value: float
    ) -> _NodeState:
        node = self.nodes.get(name)
        if node is None:
            raise UnsupportedViserMessageError(
                f"{message_type} at t={time_value:.6f} references unknown node {name!r}."
            )
        if not node.alive:
            raise UnsupportedViserMessageError(
                f"{message_type} at t={time_value:.6f} references removed node {name!r}."
            )
        return node


MessageHandler = Callable[[_NormalizerState, int, float, dict[str, Any]], None]
CreateDecoder = Callable[[dict[str, Any]], dict[str, JSONValue]]
UpdateDecoder = Callable[[_NodeState, dict[str, Any], float], dict[str, JSONValue]]


def normalize_recording(recording: RecordingPayload) -> RecordingManifest:
    state = _NormalizerState()
    for time_value, message in recording.messages:
        frame = state.frame_for(time_value)
        _apply_message(state, frame=frame, time_value=time_value, message=message)

    fps = _infer_fps(state.ordered_times)
    return RecordingManifest(
        schema_version=1,
        fps=fps,
        frame_count=max(len(state.ordered_times), 1),
        source_viser_version=recording.viser_version,
        nodes=_finalize_nodes(state),
    )


def _apply_message(
    state: _NormalizerState,
    *,
    frame: int,
    time_value: float,
    message: dict[str, Any],
) -> None:
    message_type = message.get("type")
    if not isinstance(message_type, str):
        raise UnsupportedViserMessageError(
            f"Message at t={time_value:.6f} is missing a string type."
        )
    if message_type not in SUPPORTED_MESSAGE_TYPES:
        raise UnsupportedViserMessageError(
            f"Unsupported message type {message_type!r} at t={time_value:.6f}."
        )
    if message_type in CREATE_MESSAGE_TYPES:
        _create_node(state, frame=frame, time_value=time_value, message=message)
        return
    MESSAGE_HANDLERS[message_type](state, frame, time_value, message)


def _create_node(
    state: _NormalizerState,
    *,
    frame: int,
    time_value: float,
    message: dict[str, Any],
) -> None:
    message_type = cast(str, message["type"])
    name = _require_name(message, message_type=message_type, time_value=time_value)
    if name in state.nodes:
        raise UnsupportedViserMessageError(
            f"{message_type} at t={time_value:.6f} recreates existing node {name!r}."
        )

    props = message.get("props")
    if not isinstance(props, dict):
        raise UnsupportedViserMessageError(
            f"{message_type} at t={time_value:.6f} is missing props."
        )
    kind = CREATE_MESSAGE_TYPES[message_type]
    payload = CREATE_DECODERS[kind](cast(dict[str, Any], props))
    manifest = NodeManifest(
        name=name,
        kind=kind,
        parent_name=_parent_name(name),
        create_frame=frame,
        create_payload=payload,
        transform_keyframes=[],
        visibility_keyframes=[],
        geometry_keyframes=[],
    )
    node = _NodeState(
        manifest=manifest,
        point_count=_point_count(kind, payload),
        segment_count=_segment_count(kind, payload),
    )
    state.nodes[name] = node
    state.ordered_names.append(name)
    if frame > 1:
        manifest.visibility_keyframes.append(VisibilityKeyframe(frame=1, visible=False))
        manifest.visibility_keyframes.append(
            VisibilityKeyframe(frame=frame, visible=True)
        )


def _apply_update(
    node: _NodeState,
    *,
    frame: int,
    time_value: float,
    updates: dict[str, Any],
) -> None:
    decoder = UPDATE_DECODERS.get(node.manifest.kind)
    if decoder is None:
        raise UnsupportedViserMessageError(
            f"SceneNodeUpdateMessage at t={time_value:.6f} is not supported for "
            f"{node.manifest.kind!r} nodes."
        )
    payload = decoder(node, updates, time_value)
    if frame == node.manifest.create_frame:
        node.manifest.create_payload.update(payload)
        return
    node.manifest.geometry_keyframes.append(
        GeometryKeyframe(frame=frame, payload=payload)
    )


def _decode_frame_payload(props: dict[str, Any]) -> dict[str, JSONValue]:
    return {
        "show_axes": _require_bool(props, "show_axes"),
        "axes_length": _require_float(props, "axes_length"),
        "axes_radius": _require_float(props, "axes_radius"),
        "origin_radius": _require_float(props, "origin_radius"),
        "origin_color": _decode_color(props.get("origin_color"), item_count=None),
    }


def _decode_mesh_payload(props: dict[str, Any]) -> dict[str, JSONValue]:
    return {
        "vertices": _decode_vertices(props.get("vertices")),
        "faces": _decode_faces(props.get("faces")),
        "color": _decode_color(props.get("color"), item_count=None),
        "scale": _decode_scale(props.get("scale")),
        "wireframe": _require_bool(props, "wireframe"),
        "opacity": _optional_float(props.get("opacity")),
    }


def _decode_point_cloud_payload(props: dict[str, Any]) -> dict[str, JSONValue]:
    precision = _require_str(props, "precision")
    points = _decode_point_cloud_points(props.get("points"), precision=precision)
    return {
        "points": points,
        "colors": _decode_color(props.get("colors"), item_count=len(points)),
        "point_size": _require_float(props, "point_size"),
        "point_shape": _require_str(props, "point_shape"),
        "precision": precision,
    }


def _decode_line_segments_payload(props: dict[str, Any]) -> dict[str, JSONValue]:
    points = _decode_line_points(props.get("points"))
    return {
        "points": points,
        "colors": _decode_color(
            props.get("colors"), item_count=len(points), item_width=2
        ),
        "line_width": _require_float(props, "line_width"),
    }


def _decode_icosphere_payload(props: dict[str, Any]) -> dict[str, JSONValue]:
    return {
        "radius": _require_float(props, "radius"),
        "subdivisions": _require_int(props, "subdivisions"),
        "color": _decode_color(props.get("color"), item_count=None),
        "wireframe": _require_bool(props, "wireframe"),
        "opacity": _optional_float(props.get("opacity")),
    }


def _decode_cylinder_payload(props: dict[str, Any]) -> dict[str, JSONValue]:
    return {
        "radius": _require_float(props, "radius"),
        "height": _require_float(props, "height"),
        "radial_segments": _require_int(props, "radial_segments"),
        "color": _decode_color(props.get("color"), item_count=None),
        "wireframe": _require_bool(props, "wireframe"),
        "opacity": _optional_float(props.get("opacity")),
    }


def _decode_mesh_update(
    node: _NodeState, updates: dict[str, Any], time_value: float
) -> dict[str, JSONValue]:
    return {
        "vertices": _decode_fixed_vertices(
            _decode_vertices(updates["vertices"]),
            expected_count=node.point_count,
            time_value=time_value,
            node_name=node.manifest.name,
            label="Mesh vertex",
        )
    }


def _decode_point_cloud_update(
    node: _NodeState, updates: dict[str, Any], time_value: float
) -> dict[str, JSONValue]:
    precision = cast(str, node.manifest.create_payload["precision"])
    points = _decode_point_cloud_points(updates["points"], precision=precision)
    return {
        "points": _decode_fixed_vertices(
            points,
            expected_count=node.point_count,
            time_value=time_value,
            node_name=node.manifest.name,
            label="Point",
        )
    }


def _decode_line_segments_update(
    node: _NodeState, updates: dict[str, Any], time_value: float
) -> dict[str, JSONValue]:
    points = _decode_line_points(updates["points"])
    if len(points) != node.segment_count:
        raise UnsupportedViserMessageError(
            f"Line segment count changed at t={time_value:.6f} "
            f"for {node.manifest.name!r}."
        )
    return {"points": points}


def _decode_fixed_vertices(
    values: list[list[float]],
    *,
    expected_count: int | None,
    time_value: float,
    node_name: str,
    label: str,
) -> list[list[float]]:
    if len(values) != expected_count:
        raise UnsupportedViserMessageError(
            f"{label} count changed at t={time_value:.6f} for {node_name!r}."
        )
    return values


def _decode_vertices(value: Any) -> list[list[float]]:
    return cast(list[list[float]], _decode_array(value, dtype=np.float32, dims=3))


def _decode_faces(value: Any) -> list[list[int]]:
    return cast(
        list[list[int]],
        _decode_array(value, dtype=np.uint32, dims=3, cast_int=True),
    )


def _decode_point_cloud_points(value: Any, *, precision: str) -> list[list[float]]:
    if precision == "float16":
        dtype = np.float16
    elif precision == "float32":
        dtype = np.float32
    else:
        raise UnsupportedViserMessageError(
            f"Unsupported point cloud precision {precision!r}."
        )
    return cast(list[list[float]], _decode_array(value, dtype=dtype, dims=3))


def _decode_line_points(value: Any) -> list[list[list[float]]]:
    data = _bytes_or_array(value, dtype=np.float32)
    if data.ndim == 1:
        if data.size % 6 != 0:
            raise UnsupportedViserMessageError(
                f"Expected line segment buffer with 6 values per segment, "
                f"got {data.size}."
            )
        data = data.reshape(-1, 2, 3)
    if data.ndim != 3 or data.shape[1:] != (2, 3):
        raise UnsupportedViserMessageError(
            f"Expected line segment points with shape (N, 2, 3), got {data.shape}."
        )
    return data.astype(np.float32).tolist()


def _decode_scale(value: Any) -> JSONValue:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)) and len(value) == 3:
        return [float(component) for component in value]
    raise UnsupportedViserMessageError(f"Unsupported mesh scale {value!r}.")


def _decode_color(
    value: Any,
    *,
    item_count: int | None,
    item_width: int = 1,
) -> JSONValue:
    if isinstance(value, (list, tuple)):
        components = [int(component) for component in value]
        if len(components) not in {3, 4}:
            raise UnsupportedViserMessageError(f"Unsupported color value {value!r}.")
        return components
    if not isinstance(value, (bytes, bytearray)):
        raise UnsupportedViserMessageError(f"Unsupported color value {value!r}.")
    data = np.frombuffer(value, dtype=np.uint8)
    if data.size in {3, 4}:
        return data.astype(np.uint8).tolist()
    if item_count is None:
        raise UnsupportedViserMessageError(
            f"Unsupported color buffer length {data.size}."
        )
    row_count = item_count
    for channels in (3, 4):
        if data.size == row_count * channels:
            return data.reshape(row_count, channels).astype(np.uint8).tolist()
        if item_width > 1 and data.size == row_count * item_width * channels:
            shape = (row_count, item_width, channels)
            return data.reshape(shape).astype(np.uint8).tolist()
    raise UnsupportedViserMessageError(
        f"Unsupported color buffer length {data.size} for item count {item_count}."
    )


def _decode_array(
    value: Any,
    *,
    dtype: np.dtype[Any] | type[np.generic],
    dims: int,
    cast_int: bool = False,
) -> list[list[float]] | list[list[int]]:
    data = _bytes_or_array(value, dtype=dtype)
    if data.ndim == 1:
        if data.size % dims != 0:
            raise UnsupportedViserMessageError(
                f"Expected a flat buffer divisible into {dims}-wide rows, got {data.size}."
            )
        data = data.reshape(-1, dims)
    if data.ndim != 2 or data.shape[1] != dims:
        raise UnsupportedViserMessageError(
            f"Expected an array with shape (N, {dims}), got {data.shape}."
        )
    if cast_int:
        return data.astype(np.int64).tolist()
    return data.astype(np.float32).tolist()


def _bytes_or_array(
    value: Any,
    *,
    dtype: np.dtype[Any] | type[np.generic],
) -> np.ndarray[Any, Any]:
    if isinstance(value, (bytes, bytearray)):
        return np.frombuffer(value, dtype=dtype)
    return np.asarray(value, dtype=dtype)


def _require_name(
    message: dict[str, Any],
    *,
    message_type: str,
    time_value: float,
    allow_empty: bool = False,
) -> str:
    name = message.get("name")
    if not isinstance(name, str) or (not allow_empty and not name):
        raise UnsupportedViserMessageError(
            f"{message_type} at t={time_value:.6f} is missing a valid name."
        )
    return name


def _parent_name(name: str) -> str | None:
    if name == "/":
        return None
    parent, _, _ = name.rpartition("/")
    return parent or None


def _ensure_root_node(state: _NormalizerState) -> _NodeState:
    node = state.nodes.get("")
    if node is not None:
        return node
    manifest = NodeManifest(
        name="",
        kind="root",
        parent_name=None,
        create_frame=1,
        create_payload={},
        transform_keyframes=[],
        visibility_keyframes=[],
        geometry_keyframes=[],
    )
    node = _NodeState(manifest=manifest)
    state.nodes[""] = node
    state.ordered_names.insert(0, "")
    return node


def _finalize_nodes(state: _NormalizerState) -> list[NodeManifest]:
    manifests = [state.nodes[name].manifest for name in state.ordered_names]
    if "" not in state.nodes:
        return manifests
    for manifest in manifests:
        if manifest.name and manifest.parent_name is None:
            manifest.parent_name = ""
    return manifests


def _infer_fps(times: list[float]) -> float:
    deltas = [
        curr - prev
        for prev, curr in zip(times, times[1:], strict=False)
        if curr - prev > 1e-9
    ]
    if not deltas:
        return 24.0
    return float(1.0 / median(deltas))


def _point_count(kind: str, payload: dict[str, JSONValue]) -> int | None:
    if kind == "mesh":
        return len(cast(list[list[float]], payload["vertices"]))
    if kind == "point_cloud":
        return len(cast(list[list[float]], payload["points"]))
    return None


def _segment_count(kind: str, payload: dict[str, JSONValue]) -> int | None:
    if kind == "line_segments":
        return len(cast(list[list[list[float]]], payload["points"]))
    return None


def _ignore_runtime_message(
    _state: _NormalizerState,
    _frame: int,
    time_value: float,
    message: dict[str, Any],
) -> None:
    source = message.get("source")
    if not isinstance(source, str) or not source.startswith(RUNTIME_MARKER):
        raise UnsupportedViserMessageError(
            f"Unsupported RunJavascriptMessage at t={time_value:.6f}."
        )


def _ignore_background_message(
    _state: _NormalizerState,
    _frame: int,
    time_value: float,
    message: dict[str, Any],
) -> None:
    if message.get("rgb_data") is not None or message.get("depth_data") is not None:
        raise UnsupportedViserMessageError(
            f"Unsupported non-empty BackgroundImageMessage at t={time_value:.6f}."
        )


def _ignore_message(
    _state: _NormalizerState,
    _frame: int,
    _time_value: float,
    _message: dict[str, Any],
) -> None:
    return


def _target_node(
    state: _NormalizerState,
    *,
    message_type: str,
    time_value: float,
    message: dict[str, Any],
) -> _NodeState:
    name = _require_name(
        message,
        message_type=message_type,
        time_value=time_value,
        allow_empty=True,
    )
    if name == "":
        return _ensure_root_node(state)
    return state.require_node(name, message_type=message_type, time_value=time_value)


def _handle_position_message(
    state: _NormalizerState,
    frame: int,
    time_value: float,
    message: dict[str, Any],
) -> None:
    node = _target_node(
        state, message_type="SetPositionMessage", time_value=time_value, message=message
    )
    position = _vector3(
        message.get("position"), field="position", message_type="SetPositionMessage"
    )
    node.manifest.transform_keyframes.append(
        TransformKeyframe(frame=frame, position=position)
    )


def _handle_orientation_message(
    state: _NormalizerState,
    frame: int,
    time_value: float,
    message: dict[str, Any],
) -> None:
    node = _target_node(
        state,
        message_type="SetOrientationMessage",
        time_value=time_value,
        message=message,
    )
    rotation = _vector4(
        message.get("wxyz"), field="wxyz", message_type="SetOrientationMessage"
    )
    node.manifest.transform_keyframes.append(
        TransformKeyframe(frame=frame, rotation_wxyz=rotation)
    )


def _handle_visibility_message(
    state: _NormalizerState,
    frame: int,
    time_value: float,
    message: dict[str, Any],
) -> None:
    node = _target_node(
        state,
        message_type="SetSceneNodeVisibilityMessage",
        time_value=time_value,
        message=message,
    )
    visible = message.get("visible")
    if not isinstance(visible, bool):
        raise UnsupportedViserMessageError(
            f"SetSceneNodeVisibilityMessage at t={time_value:.6f} "
            f"has invalid visible={visible!r}."
        )
    node.manifest.visibility_keyframes.append(
        VisibilityKeyframe(frame=frame, visible=visible)
    )


def _handle_remove_message(
    state: _NormalizerState,
    frame: int,
    time_value: float,
    message: dict[str, Any],
) -> None:
    node = _target_node(
        state,
        message_type="RemoveSceneNodeMessage",
        time_value=time_value,
        message=message,
    )
    node.alive = False
    node.manifest.visibility_keyframes.append(
        VisibilityKeyframe(frame=frame, visible=False)
    )


def _handle_scene_update_message(
    state: _NormalizerState,
    frame: int,
    time_value: float,
    message: dict[str, Any],
) -> None:
    node = _target_node(
        state,
        message_type="SceneNodeUpdateMessage",
        time_value=time_value,
        message=message,
    )
    updates = message.get("updates")
    if not isinstance(updates, dict):
        raise UnsupportedViserMessageError(
            f"SceneNodeUpdateMessage at t={time_value:.6f} is missing updates."
        )
    _apply_update(
        node, frame=frame, time_value=time_value, updates=cast(dict[str, Any], updates)
    )


def _require_bool(props: dict[str, Any], key: str) -> bool:
    value = props.get(key)
    if not isinstance(value, bool):
        raise UnsupportedViserMessageError(f"Expected bool {key!r}, got {value!r}.")
    return value


def _require_float(props: dict[str, Any], key: str) -> float:
    value = props.get(key)
    if not isinstance(value, (int, float)):
        raise UnsupportedViserMessageError(f"Expected float {key!r}, got {value!r}.")
    return float(value)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if not isinstance(value, (int, float)):
        raise UnsupportedViserMessageError(f"Expected optional float, got {value!r}.")
    return float(value)


def _require_int(props: dict[str, Any], key: str) -> int:
    value = props.get(key)
    if not isinstance(value, int):
        raise UnsupportedViserMessageError(f"Expected int {key!r}, got {value!r}.")
    return value


def _require_str(props: dict[str, Any], key: str) -> str:
    value = props.get(key)
    if not isinstance(value, str):
        raise UnsupportedViserMessageError(f"Expected str {key!r}, got {value!r}.")
    return value


def _vector3(value: Any, *, field: str, message_type: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise UnsupportedViserMessageError(
            f"{message_type} has invalid {field}={value!r}; expected length 3."
        )
    return [float(component) for component in value]


def _vector4(value: Any, *, field: str, message_type: str) -> list[float]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise UnsupportedViserMessageError(
            f"{message_type} has invalid {field}={value!r}; expected length 4."
        )
    return [float(component) for component in value]


CREATE_DECODERS: dict[str, CreateDecoder] = {
    "frame": _decode_frame_payload,
    "mesh": _decode_mesh_payload,
    "point_cloud": _decode_point_cloud_payload,
    "line_segments": _decode_line_segments_payload,
    "icosphere": _decode_icosphere_payload,
    "cylinder": _decode_cylinder_payload,
}

UPDATE_DECODERS: dict[str, UpdateDecoder] = {
    "mesh": _decode_mesh_update,
    "point_cloud": _decode_point_cloud_update,
    "line_segments": _decode_line_segments_update,
}

MESSAGE_HANDLERS: dict[str, MessageHandler] = {
    "RunJavascriptMessage": _ignore_runtime_message,
    "BackgroundImageMessage": _ignore_background_message,
    "SetGuiPanelLabelMessage": _ignore_message,
    "SetPositionMessage": _handle_position_message,
    "SetOrientationMessage": _handle_orientation_message,
    "SetSceneNodeVisibilityMessage": _handle_visibility_message,
    "RemoveSceneNodeMessage": _handle_remove_message,
    "SceneNodeUpdateMessage": _handle_scene_update_message,
}
