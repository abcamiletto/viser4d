from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, cast


MIN_BPY_VERSION = (5, 0, 0)


def convert_manifest(payload: dict[str, Any], output_path: Path) -> None:
    bpy = _import_bpy()
    nodes = cast(list[dict[str, Any]], payload["nodes"])

    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.fps = max(int(round(float(payload["fps"]))), 1)
    scene.frame_start = 1
    scene.frame_end = int(payload["frame_count"])

    objects = {node["name"]: _create_node_object(bpy, node) for node in nodes}

    implicit_parents: dict[str, Any] = {}
    for node in nodes:
        parent_name = node["parent_name"]
        parent = (
            _ensure_parent_object(
                bpy,
                parent_name,
                explicit_objects=objects,
                implicit_objects=implicit_parents,
            )
            if parent_name is not None
            else None
        )
        if parent is not None:
            objects[node["name"]].parent = parent

    for node in nodes:
        obj = objects[node["name"]]
        _apply_transform_keyframes(obj, node)
        _apply_visibility_keyframes(obj, node)
        _apply_geometry_keyframes(obj, node)

    _set_linear_interpolation(bpy)
    bpy.ops.wm.save_as_mainfile(filepath=str(output_path))


def _import_bpy() -> Any:
    try:
        import bpy  # ty: ignore[unresolved-import]
    except ImportError as exc:
        raise RuntimeError(
            "viser4d-to-blend requires the bpy module. Install bpy>5.0.0."
        ) from exc

    version = tuple(cast(tuple[int, int, int], bpy.app.version))
    if version <= MIN_BPY_VERSION:
        found = ".".join(str(part) for part in version)
        raise RuntimeError(f"viser4d-to-blend requires bpy>5.0.0. Found bpy {found}.")
    return bpy


def _safe_name(name: str) -> str:
    stripped = name.strip("/")
    return stripped.replace("/", "__") or "root"


def _payload(node: dict[str, Any]) -> dict[str, Any]:
    return cast(dict[str, Any], node["create_payload"])


def _first_color_leaf(
    color: list[int] | list[list[int]] | list[list[list[int]]],
) -> list[int]:
    sample: Any = color
    while sample and isinstance(sample[0], list):
        sample = sample[0]
    return cast(list[int], sample)


def _ensure_material(
    bpy: Any,
    name: str,
    color: list[int] | list[list[int]] | list[list[list[int]]] | None,
) -> Any | None:
    if color is None:
        return None
    rgba = _first_color_leaf(color)
    material = bpy.data.materials.new(name=name)
    material.use_nodes = True
    normalized = [float(channel) / 255.0 for channel in rgba]
    if len(normalized) == 3:
        normalized.append(1.0)
    base_color = material.node_tree.nodes["Principled BSDF"].inputs["Base Color"]
    base_color.default_value = normalized
    return material


def _create_frame_object(bpy: Any, node: dict[str, Any]) -> Any:
    bpy.ops.object.empty_add(type="ARROWS")
    obj = bpy.context.active_object
    assert obj is not None
    obj.empty_display_size = float(_payload(node)["axes_length"])
    return obj


def _create_root_object(bpy: Any, _node: dict[str, Any]) -> Any:
    bpy.ops.object.empty_add(type="PLAIN_AXES")
    obj = bpy.context.active_object
    assert obj is not None
    return obj


def _create_mesh_object(
    bpy: Any,
    name: str,
    vertices: list[list[float]],
    faces: list[list[int]],
    edges: list[tuple[int, int]] | None = None,
) -> Any:
    mesh = bpy.data.meshes.new(name)
    mesh.from_pydata(vertices, edges or [], faces)
    mesh.update()
    obj = bpy.data.objects.new(name, mesh)
    bpy.context.scene.collection.objects.link(obj)
    return obj


def _create_mesh_node(bpy: Any, node: dict[str, Any]) -> Any:
    payload = _payload(node)
    name = _safe_name(str(node["name"]))
    return _create_mesh_object(bpy, name, payload["vertices"], payload["faces"])


def _create_point_cloud_node(bpy: Any, node: dict[str, Any]) -> Any:
    payload = _payload(node)
    name = _safe_name(str(node["name"]))
    return _create_mesh_object(bpy, name, payload["points"], [])


def _create_line_segments_node(bpy: Any, node: dict[str, Any]) -> Any:
    payload = _payload(node)
    name = _safe_name(str(node["name"]))
    line_points = cast(list[list[list[float]]], payload["points"])
    vertices = [endpoint for segment in line_points for endpoint in segment]
    edges = [(index, index + 1) for index in range(0, len(vertices), 2)]
    return _create_mesh_object(bpy, name, vertices, [], edges)


def _create_icosphere_node(bpy: Any, node: dict[str, Any]) -> Any:
    payload = _payload(node)
    bpy.ops.mesh.primitive_ico_sphere_add(
        subdivisions=int(payload["subdivisions"]),
        radius=float(payload["radius"]),
    )
    obj = bpy.context.active_object
    assert obj is not None
    return obj


def _create_cylinder_node(bpy: Any, node: dict[str, Any]) -> Any:
    payload = _payload(node)
    bpy.ops.mesh.primitive_cylinder_add(
        vertices=int(payload["radial_segments"]),
        radius=float(payload["radius"]),
        depth=float(payload["height"]),
    )
    obj = bpy.context.active_object
    assert obj is not None
    return obj


def _create_node_object(bpy: Any, node: dict[str, Any]) -> Any:
    kind = cast(str, node["kind"])
    obj = OBJECT_BUILDERS[kind](bpy, node)
    payload = _payload(node)
    name = _safe_name(str(node["name"]))

    obj.name = name
    obj["viser_name"] = str(node["name"])
    obj.rotation_mode = "QUATERNION"
    color = payload.get("color") or payload.get("colors") or payload.get("origin_color")
    material = _ensure_material(bpy, f"{name}_material", color)
    if material is not None and obj.type == "MESH":
        obj.data.materials.append(material)
    return obj


def _apply_transform_keyframes(obj: Any, node: dict[str, Any]) -> None:
    for keyframe in cast(list[dict[str, Any]], node["transform_keyframes"]):
        frame = int(keyframe["frame"])
        position = keyframe.get("position")
        rotation = keyframe.get("rotation_wxyz")
        if position is not None:
            obj.location = position
            obj.keyframe_insert(data_path="location", frame=frame)
        if rotation is not None:
            obj.rotation_quaternion = rotation
            obj.keyframe_insert(data_path="rotation_quaternion", frame=frame)


def _apply_visibility_keyframes(obj: Any, node: dict[str, Any]) -> None:
    create_frame = int(node["create_frame"])
    if create_frame > 1:
        obj.hide_viewport = True
        obj.hide_render = True
        obj.keyframe_insert(data_path="hide_viewport", frame=1)
        obj.keyframe_insert(data_path="hide_render", frame=1)
    for keyframe in cast(list[dict[str, Any]], node["visibility_keyframes"]):
        frame = int(keyframe["frame"])
        visible = bool(keyframe["visible"])
        obj.hide_viewport = not visible
        obj.hide_render = not visible
        obj.keyframe_insert(data_path="hide_viewport", frame=frame)
        obj.keyframe_insert(data_path="hide_render", frame=frame)


def _ensure_parent_object(
    bpy: Any,
    name: str,
    *,
    explicit_objects: dict[str, Any],
    implicit_objects: dict[str, Any],
) -> Any | None:
    if not name:
        return None
    if name in explicit_objects:
        return explicit_objects[name]
    existing = implicit_objects.get(name)
    if existing is not None:
        return existing

    bpy.ops.object.empty_add(type="PLAIN_AXES")
    obj = bpy.context.active_object
    assert obj is not None
    obj.name = _safe_name(name)
    obj["viser_name"] = name
    parent_name = name.rsplit("/", 1)[0] or None
    if parent_name is not None:
        parent = _ensure_parent_object(
            bpy,
            parent_name,
            explicit_objects=explicit_objects,
            implicit_objects=implicit_objects,
        )
        if parent is not None:
            obj.parent = parent
    implicit_objects[name] = obj
    return obj


def _apply_geometry_keyframes(obj: Any, node: dict[str, Any]) -> None:
    keyframes = cast(list[dict[str, Any]], node["geometry_keyframes"])
    if not keyframes:
        return

    basis = obj.shape_key_add(name="Basis", from_mix=False)
    basis.interpolation = "KEY_LINEAR"
    previous_name: str | None = None
    for geometry in keyframes:
        frame = int(geometry["frame"])
        vertices = GEOMETRY_VERTICES[cast(str, node["kind"])](geometry["payload"])
        shape_key = obj.shape_key_add(name=f"frame_{frame:04d}", from_mix=False)
        shape_key.interpolation = "KEY_LINEAR"
        for vertex, coord in zip(shape_key.data, vertices, strict=True):
            vertex.co = coord
        shape_key.value = 0.0
        shape_key.keyframe_insert(data_path="value", frame=max(frame - 1, 1))
        shape_key.value = 1.0
        shape_key.keyframe_insert(data_path="value", frame=frame)
        shape_key.value = 0.0
        shape_key.keyframe_insert(data_path="value", frame=frame + 1)
        if previous_name is not None:
            previous = obj.data.shape_keys.key_blocks[previous_name]
            previous.value = 0.0
            previous.keyframe_insert(data_path="value", frame=frame)
        previous_name = shape_key.name


def _set_linear_interpolation(bpy: Any) -> None:
    for action in bpy.data.actions:
        for curve in _action_fcurves(action):
            for keyframe in curve.keyframe_points:
                keyframe.interpolation = "LINEAR"


def _action_fcurves(action: Any) -> Any:
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return fcurves

    layered_fcurves: list[Any] = []
    for layer in action.layers:
        for strip in layer.strips:
            for channelbag in strip.channelbags:
                layered_fcurves.extend(channelbag.fcurves)
    return layered_fcurves


def _mesh_vertices(payload: dict[str, Any]) -> list[list[float]]:
    return cast(list[list[float]], payload["vertices"])


def _point_cloud_vertices(payload: dict[str, Any]) -> list[list[float]]:
    return cast(list[list[float]], payload["points"])


def _line_segment_vertices(payload: dict[str, Any]) -> list[list[float]]:
    segments = cast(list[list[list[float]]], payload["points"])
    return [endpoint for segment in segments for endpoint in segment]


ObjectBuilder = Callable[[Any, dict[str, Any]], Any]
GeometryExtractor = Callable[[dict[str, Any]], list[list[float]]]

OBJECT_BUILDERS: dict[str, ObjectBuilder] = {
    "frame": _create_frame_object,
    "root": _create_root_object,
    "mesh": _create_mesh_node,
    "point_cloud": _create_point_cloud_node,
    "line_segments": _create_line_segments_node,
    "icosphere": _create_icosphere_node,
    "cylinder": _create_cylinder_node,
}

GEOMETRY_VERTICES: dict[str, GeometryExtractor] = {
    "mesh": _mesh_vertices,
    "point_cloud": _point_cloud_vertices,
    "line_segments": _line_segment_vertices,
}
