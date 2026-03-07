from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import warnings


CLIENT_FILES = (
    "binary.ts",
    "build-runtime.mjs",
    "index.ts",
    "package-lock.json",
    "package.json",
    "runtime.ts",
    "tsconfig.json",
)


def client_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent / "client"


def build_inputs() -> list[pathlib.Path] | None:
    paths = [client_dir() / name for name in CLIENT_FILES]
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


def _bundle_is_stale(
    runtime_path: pathlib.Path, inputs: list[pathlib.Path]
) -> bool:
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
    npm_path = node_bin_dir / "npm"
    node_path = node_bin_dir / "node"
    if sys.platform == "win32":
        npm_path = npm_path.with_suffix(".cmd")
        node_path = node_path.with_suffix(".exe")

    env = os.environ.copy()
    env["NODE_VIRTUAL_ENV"] = str(node_bin_dir.parent)
    env["PATH"] = str(node_bin_dir) + (os.pathsep + env["PATH"])

    cwd = client_dir()
    install_cmd = ["ci"] if (cwd / "package-lock.json").exists() else ["install"]
    subprocess.run([str(npm_path), *install_cmd], cwd=cwd, env=env, check=True)
    subprocess.run([str(node_path), "build-runtime.mjs"], cwd=cwd, env=env, check=True)
