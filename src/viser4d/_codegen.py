"""Generate ``client/protocol.gen.ts`` from the ``_protocol.py`` definitions.

Run with ``python -m viser4d._codegen``. The client build (``build-runtime.mjs``)
invokes this before bundling, so the wire protocol has a single source of truth
in Python.
"""

from __future__ import annotations

import dataclasses
import types
from collections import defaultdict
from pathlib import Path
from typing import Any, Union, cast

import numpy as np
from typing_extensions import get_args, get_origin, get_type_hints, is_typeddict

from . import _protocol

_HEADER = (
    "// AUTOMATICALLY GENERATED from viser4d/_protocol.py -- do not edit.\n"
    "// Regenerate with: python -m viser4d._codegen\n"
)

_TYPE_MAPPING: dict[object, str] = {
    bool: "boolean",
    float: "number",
    int: "number",
    str: "string",
    np.ndarray: "Uint8Array<ArrayBuffer>",
    Any: "any",
    type(None): "null",
    _protocol.ScenePayload: 'import("./binary").ScenePayload',
}


def output_path() -> Path:
    return Path(__file__).resolve().parent / "client" / "protocol.gen.ts"


def generate_typescript() -> str:
    message_types = [
        cls
        for cls in _protocol._TimelineMessage.get_subclasses()
        if dataclasses.is_dataclass(cls)
    ]

    lines: list[str] = [_HEADER]
    tag_map: defaultdict[str, list[str]] = defaultdict(list)
    for cls in message_types:
        for tag in getattr(cls, "_tags", ()):
            tag_map[tag].append(cls.__name__)

        docstring = _docstring(cls)
        if docstring is not None:
            lines.append(f"/** {docstring} */")
        lines.append(f"export interface {cls.__name__} {{")
        lines.append(f'  type: "{cls.__name__}";')
        hints = get_type_hints(cls)
        for field in dataclasses.fields(cls):
            lines.append(f"  {field.name}: {_ts_type(hints[field.name])};")
        lines.append("}")
        lines.append("")

    for tag, class_names in tag_map.items():
        lines.append(f"export type {tag} =")
        lines.extend(f"  | {name}" for name in class_names)
        lines[-1] += ";"
        lines.append(f"const _{tag}Types = new Set({sorted(class_names)!r});")
        lines.append(
            f"export function is{tag}(message: {{ type: string }}): "
            f"message is {tag} {{\n  return _{tag}Types.has(message.type);\n}}"
        )
        lines.append("")

    return "\n".join(lines)


def _docstring(cls: type[Any]) -> str | None:
    docstring = (cls.__doc__ or "").strip()
    if not docstring or docstring.startswith(f"{cls.__name__}("):
        return None
    return " ".join(line.strip() for line in docstring.splitlines())


def _ts_type(typ: Any) -> str:
    origin = get_origin(typ)
    if origin is list:
        return _ts_type(get_args(typ)[0]) + "[]"
    if origin is dict:
        key_type, value_type = get_args(typ)
        return f"{{ [key: {_ts_type(key_type)}]: {_ts_type(value_type)} }}"
    if origin is tuple:
        return "[" + ", ".join(_ts_type(arg) for arg in get_args(typ)) + "]"
    if origin in (Union, types.UnionType):
        return " | ".join(dict.fromkeys(_ts_type(arg) for arg in get_args(typ)))
    if is_typeddict(typ):
        hints = get_type_hints(typ)
        fields = ", ".join(f"{name}: {_ts_type(t)}" for name, t in hints.items())
        return "{ " + fields + " }"

    raw = cast(Any, getattr(typ, "__origin__", typ))
    if raw is np.ndarray or (isinstance(raw, type) and issubclass(raw, np.ndarray)):
        return _TYPE_MAPPING[np.ndarray]
    if raw not in _TYPE_MAPPING:
        raise TypeError(f"Unsupported protocol field type: {typ!r}")
    return _TYPE_MAPPING[raw]


if __name__ == "__main__":
    output_path().write_text(generate_typescript())
    print(f"Wrote {output_path()}")
