from __future__ import annotations

import pathlib
import shutil
import subprocess
import warnings

BUILD_INPUTS = (
    "package.json",
    "pnpm-lock.yaml",
    "scripts/build-runtime.mjs",
    "src/viser4d/client/binary.ts",
    "src/viser4d/client/index.ts",
    "src/viser4d/client/runtime.ts",
)


def runtime_repo_root(runtime_path: pathlib.Path) -> pathlib.Path:
    return runtime_path.parent.parent.parent


def runtime_build_inputs(runtime_path: pathlib.Path) -> list[pathlib.Path] | None:
    repo_root = runtime_repo_root(runtime_path)
    build_inputs = [repo_root / path for path in BUILD_INPUTS]
    if not all(path.exists() for path in build_inputs):
        return None
    return build_inputs


def runtime_bundle_is_stale(
    runtime_path: pathlib.Path, build_inputs: list[pathlib.Path]
) -> bool:
    if not runtime_path.exists():
        return True
    runtime_mtime = runtime_path.stat().st_mtime
    return any(path.stat().st_mtime > runtime_mtime for path in build_inputs)


def ensure_runtime_bundle(runtime_path: pathlib.Path) -> None:
    build_inputs = runtime_build_inputs(runtime_path)
    if build_inputs is None or not runtime_bundle_is_stale(runtime_path, build_inputs):
        return

    pnpm = shutil.which("pnpm")
    if pnpm is None:
        if runtime_path.exists():
            warnings.warn(
                "viser4d runtime sources changed, but `pnpm` is unavailable. "
                "Using the existing generated bundle.",
                stacklevel=2,
            )
            return
        raise RuntimeError(
            "Missing generated client bundle at src/viser4d/runtime.js and `pnpm` "
            "is unavailable to rebuild it."
        )

    repo_root = runtime_repo_root(runtime_path)
    subprocess.run([pnpm, "install", "--frozen-lockfile"], cwd=repo_root, check=True)
    subprocess.run([pnpm, "run", "build:runtime"], cwd=repo_root, check=True)
