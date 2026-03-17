from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def test_blender_cli_validates_basic_fixture(tmp_path: Path) -> None:
    output_path = tmp_path / "basic.blend"
    manifest_path = tmp_path / "basic_manifest.json"

    result = _run_blender_cli(
        ASSETS_DIR / "blender_basic_mesh.viser",
        output_path,
        "--validate-only",
        "--emit-manifest",
        str(manifest_path),
        "--overwrite",
    )

    assert result.returncode == 0, result.stderr
    manifest = json.loads(manifest_path.read_text())
    assert manifest["fps"] == 30.0
    assert any(node["kind"] == "mesh" for node in manifest["nodes"])
    assert any(node["kind"] == "frame" for node in manifest["nodes"])


def test_blender_cli_runs_full_conversion_with_real_bpy(tmp_path: Path) -> None:
    output_path = tmp_path / "converted.blend"
    result = _run_blender_cli(
        ASSETS_DIR / "blender_basic_mesh.viser",
        output_path,
        "--overwrite",
    )

    assert result.returncode == 0, result.stderr
    assert output_path.exists()
    assert output_path.stat().st_size > 0
    assert b"BLENDER" in output_path.read_bytes()[:128]


def test_blender_cli_rejects_unsupported_fixture(tmp_path: Path) -> None:
    output_path = tmp_path / "unsupported.blend"

    result = _run_blender_cli(
        ASSETS_DIR / "blender_unsupported_audio.viser",
        output_path,
        "--validate-only",
    )

    assert result.returncode != 0
    assert "Unsupported message type 'AddAudioMessage'" in result.stderr


def _run_blender_cli(
    input_path: Path,
    output_path: Path,
    *extra_args: str,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-m",
        "viser4d_blender",
        str(input_path),
        str(output_path),
        *extra_args,
    ]
    return subprocess.run(command, capture_output=True, text=True)
