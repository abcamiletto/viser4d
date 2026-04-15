from __future__ import annotations

import dataclasses
import types
from collections import defaultdict
from typing import Any, Literal, Type, Union, cast

import numpy as np
from typing_extensions import (
    Annotated,
    Never,
    NotRequired,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)


@dataclasses.dataclass(frozen=True)
class TypeScriptAnnotationOverride:
    annotation: str


def _typescript_docstring(cls: type[Any]) -> str | None:
    docstring = cls.__doc__
    if docstring is None:
        return None
    stripped = docstring.strip()
    if dataclasses.is_dataclass(cls) and stripped.startswith(f"{cls.__name__}("):
        return None
    if not stripped:
        return None
    return stripped


def generate_typescript_interfaces(
    message_cls: Type[Any],
    *,
    raw_type_mapping: dict[object, str] | None = None,
) -> str:
    message_types = [
        cls
        for cls in message_cls.get_subclasses()
        if dataclasses.is_dataclass(cls) or is_typeddict(cls)
    ]

    type_mapping = {
        bool: "boolean",
        float: "number",
        int: "number",
        str: "string",
        np.ndarray: "Uint8Array<ArrayBuffer>",
        bytes: "Uint8Array<ArrayBuffer>",
        Any: "any",
        None: "null",
        Never: "never",
        type(None): "null",
    }
    if raw_type_mapping is not None:
        for python_type, typescript_type in raw_type_mapping.items():
            type_mapping[python_type] = typescript_type

    numpy_dtype_to_ts_typed_array = {
        np.float16: "Uint16Array",
        np.float32: "Float32Array",
        np.float64: "Float64Array",
        np.uint8: "Uint8Array<ArrayBuffer>",
        np.uint16: "Uint16Array",
        np.uint32: "Uint32Array",
        np.int8: "Int8Array",
        np.int16: "Int16Array",
        np.int32: "Int32Array",
    }

    def get_ts_type(typ: Type[Any]) -> str:
        origin_typ = get_origin(typ)
        if origin_typ is Annotated:
            args = get_args(typ)
            for arg in args[1:]:
                if isinstance(arg, TypeScriptAnnotationOverride):
                    return arg.annotation
            origin_typ = args[0]

        UnionType = getattr(types, "UnionType", Union)
        if origin_typ is tuple:
            args = get_args(typ)
            if len(args) == 2 and args[1] == ...:
                return get_ts_type(args[0]) + "[]"
            return "[" + ", ".join(map(get_ts_type, args)) + "]"
        if origin_typ is list:
            return get_ts_type(get_args(typ)[0]) + "[]"
        if origin_typ is dict:
            key_type, value_type = get_args(typ)
            return (
                "{[key: "
                + get_ts_type(key_type)
                + "]: "
                + get_ts_type(value_type)
                + "}"
            )
        if origin_typ is Literal:
            return " | ".join(
                repr(value).lower() if type(value) is bool else repr(value)
                for value in get_args(typ)
            )
        if origin_typ in (Union, UnionType):
            return (
                "("
                + " | ".join({get_ts_type(arg): None for arg in get_args(typ)}.keys())
                + ")"
            )
        if is_typeddict(typ) or dataclasses.is_dataclass(typ):
            hints = get_type_hints(typ)
            if dataclasses.is_dataclass(typ):
                hints = {
                    field.name: hints[field.name] for field in dataclasses.fields(typ)
                }
            optional_keys = getattr(typ, "__optional_keys__", [])

            def fmt(key: str) -> str:
                value_type = hints[key]
                optional = key in optional_keys
                if is_typeddict(typ) and get_origin(value_type) is NotRequired:
                    value_type = get_args(value_type)[0]
                return f"'{key}'{'?' if optional else ''}: {get_ts_type(value_type)}"

            return "{" + ", ".join(map(fmt, hints)) + "}"

        raw_typ = cast(Any, getattr(typ, "__origin__", typ))
        if raw_typ is np.ndarray:
            args = get_args(typ)
            if args:
                dtype_arg = args[-1]
                dtype_args = get_args(dtype_arg)
                if dtype_args and dtype_args[0] in numpy_dtype_to_ts_typed_array:
                    return numpy_dtype_to_ts_typed_array[dtype_args[0]]
        assert raw_typ in type_mapping, f"Unsupported type {raw_typ}"
        return type_mapping[raw_typ]

    out_lines: list[str] = []
    tag_map = defaultdict(list)

    for cls in message_types:
        docstring = _typescript_docstring(cls)
        if docstring is not None:
            docstring = "\n * ".join(line.strip() for line in docstring.split("\n"))
            out_lines.append(f"/** {docstring}")
            out_lines.append(" *")
            out_lines.append(" * (automatically generated)")
            out_lines.append(" */")

        for tag in getattr(cls, "_tags", []):
            tag_map[tag].append(cls.__name__)

        out_lines.append(f"export interface {cls.__name__} " + "{")
        out_lines.append(f'  type: "{cls.__name__}";')
        field_names = {field.name for field in dataclasses.fields(cls)}
        for name, typ in get_type_hints(cls, include_extras=True).items():
            if name in field_names:
                out_lines.append(f"  {name}: {get_ts_type(typ)};")
        out_lines.append("}")
    out_lines.append("")

    out_lines.append("export type Message = ")
    for cls in message_types:
        out_lines.append(f"  | {cls.__name__}")
    out_lines[-1] += ";"

    for tag, cls_names in tag_map.items():
        out_lines.append(f"export type {tag} = ")
        for cls_name in cls_names:
            out_lines.append(f"  | {cls_name}")
        out_lines[-1] += ";"

    for tag, cls_names in tag_map.items():
        out_lines.append(
            f"const typeSet{tag} = new Set(['" + "', '".join(cls_names) + "']);"
        )
        out_lines.append(
            f"export function is{tag}(message: {{ type: string }}): message is {tag} "
            + "{"
        )
        out_lines.append(f"  return typeSet{tag}.has(message.type);")
        out_lines.append("}")

    return "\n".join(
        [
            "// AUTOMATICALLY GENERATED message interfaces, from Python dataclass definitions.",
            "// This file should not be manually modified.",
            *out_lines,
            "",
        ]
    )
