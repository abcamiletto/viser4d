from __future__ import annotations

import argparse
import json
from pathlib import Path

from ._bpy_backend import convert_manifest
from ._normalize import normalize_recording
from ._recording import load_viser_recording


def main() -> None:
    args = _parse_args()
    manifest = normalize_recording(load_viser_recording(args.input_path)).to_jsonable()

    if args.emit_manifest is not None:
        _check_writable(args.emit_manifest, overwrite=args.overwrite)
        args.emit_manifest.write_text(json.dumps(manifest, indent=2))

    if args.validate_only:
        return

    _check_writable(args.output_path, overwrite=args.overwrite)
    convert_manifest(manifest, args.output_path)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="viser4d-to-blend",
        description=(
            "Convert a strict supported subset of .viser recordings into .blend "
            "files. Requires bpy>5.0.0."
        ),
    )
    parser.add_argument("input_path", type=Path, help="Input .viser recording.")
    parser.add_argument("output_path", type=Path, help="Output .blend path.")
    parser.add_argument(
        "--emit-manifest",
        type=Path,
        default=None,
        help="Write the normalized manifest JSON for inspection.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate and normalize the .viser file without invoking Blender.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Allow overwriting output files.",
    )
    return parser.parse_args()


def _check_writable(path: Path, *, overwrite: bool) -> None:
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists. Use --overwrite to replace it.")
