from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import warnings


CLIENT_SOURCE_SUFFIXES = (".ts", ".json", ".mjs")
CLIENT_IGNORED_DIRS = {".nodeenv", "node_modules"}


def client_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent / "client"


def project_root() -> pathlib.Path | None:
    for path in pathlib.Path(__file__).resolve().parents:
        if (path / "package.json").exists() and (path / "pnpm-lock.yaml").exists():
            return path
    return None


def client_sources() -> list[pathlib.Path]:
    root = client_dir()
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in CLIENT_SOURCE_SUFFIXES
        and not any(part in CLIENT_IGNORED_DIRS for part in path.parts)
    )


def build_inputs() -> list[pathlib.Path] | None:
    root = project_root()
    if root is None:
        return None
    paths = client_sources()
    paths.extend((root / "package.json", root / "pnpm-lock.yaml"))
    if not all(path.exists() for path in paths):
        return None
    return paths


def bundle_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent / "runtime.js"


def ensure_client_is_built() -> None:
    inputs = build_inputs()
    runtime_path = bundle_path()
    if inputs is None or not _bundle_is_stale(runtime_path, inputs):
        return

    try:
        node_bin_dir = _install_sandboxed_node()
        _build_client(node_bin_dir)
    except (RuntimeError, subprocess.CalledProcessError):
        if runtime_path.exists():
            warnings.warn(
                "viser4d client sources changed, but the bundle could not be rebuilt. "
                "Using the existing generated bundle.",
                stacklevel=2,
            )
            return
        raise RuntimeError(
            "Missing generated client bundle at src/viser4d/runtime.js and viser4d "
            "could not rebuild it automatically."
        )


def _bundle_is_stale(runtime_path: pathlib.Path, inputs: list[pathlib.Path]) -> bool:
    if not runtime_path.exists():
        return True
    runtime_mtime = runtime_path.stat().st_mtime
    return any(path.stat().st_mtime > runtime_mtime for path in inputs)


def _install_sandboxed_node() -> pathlib.Path:
    env_dir = client_dir() / ".nodeenv"
    node_bin_dir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    npm_path = node_bin_dir / "npm"
    if sys.platform == "win32":
        npm_path = npm_path.with_suffix(".cmd")
    if npm_path.exists():
        return node_bin_dir
    if env_dir.exists():
        shutil.rmtree(env_dir)

    try:
        import nodeenv  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "nodeenv is required to bootstrap Node.js. Install viser4d with its "
            "runtime dependencies."
        ) from None

    result = subprocess.run(
        [sys.executable, "-m", "nodeenv", "--node=24.12.0", str(env_dir)],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to install Node.js using nodeenv.")
    return node_bin_dir


def _build_client(node_bin_dir: pathlib.Path) -> None:
    corepack_path = node_bin_dir / "corepack"
    node_path = node_bin_dir / "node"
    if sys.platform == "win32":
        corepack_path = corepack_path.with_suffix(".cmd")
        node_path = node_path.with_suffix(".exe")

    env = os.environ.copy()
    env["NODE_VIRTUAL_ENV"] = str(node_bin_dir.parent)
    env["PATH"] = str(node_bin_dir) + (os.pathsep + env["PATH"])

    root = project_root()
    if root is None:
        raise RuntimeError("Could not locate package.json and pnpm-lock.yaml.")
    subprocess.run(
        [str(corepack_path), "pnpm", "install", "--frozen-lockfile"],
        cwd=root,
        env=env,
        check=True,
    )
    subprocess.run(
        [str(node_path), "src/viser4d/client/build-runtime.mjs"],
        cwd=root,
        env=env,
        check=True,
    )
