from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import warnings


CLIENT_SOURCE_SUFFIXES = (".ts", ".json", ".mjs")
CLIENT_IGNORED_DIRS = {".nodeenv", "node_modules"}


def client_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent / "client"


def _is_editable_install() -> bool:
    package_dir = pathlib.Path(__file__).resolve().parent
    return package_dir.name == "viser4d" and package_dir.parent.name == "src"


def bundle_path() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent / "runtime.js"


def ensure_client_is_built() -> None:
    """Ensure ``runtime.js`` exists and is up to date enough to load."""
    client_root = client_dir()
    runtime_path = bundle_path()
    package_json_path = client_root / "package.json"
    missing_bundle_message = (
        "Missing generated client bundle at src/viser4d/runtime.js and viser4d "
        "could not rebuild it automatically."
    )
    runtime_exists = runtime_path.exists()
    if runtime_exists:
        has_client_sources = package_json_path.exists()
        bundle_is_fresh = has_client_sources and (
            _modified_time_recursive(client_root) <= runtime_path.stat().st_mtime
        )
        can_use_existing_bundle = (
            not _is_editable_install() or not has_client_sources or bundle_is_fresh
        )
        if can_use_existing_bundle:
            return

    try:
        _build_client()
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        if not runtime_path.exists():
            raise RuntimeError(missing_bundle_message) from exc
        warnings.warn(
            "viser4d client sources changed, but the bundle could not be rebuilt. "
            "Using the existing generated bundle.",
            stacklevel=2,
        )


def _modified_time_recursive(root: pathlib.Path) -> float:
    """Return the newest source-file mtime under ``root``."""
    return max(
        path.stat().st_mtime
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix in CLIENT_SOURCE_SUFFIXES
        and not any(part in CLIENT_IGNORED_DIRS for part in path.parts)
    )


def _install_sandboxed_node() -> pathlib.Path:
    env_dir = client_dir() / ".nodeenv"
    node_bin_dir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    npx_path = node_bin_dir / "npx"
    if sys.platform == "win32":
        npx_path = npx_path.with_suffix(".cmd")
    if npx_path.exists():
        return node_bin_dir

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


def _build_client() -> None:
    node_bin_dir = _install_sandboxed_node()
    npm_path = node_bin_dir / "npm"
    if sys.platform == "win32":
        npm_path = npm_path.with_suffix(".cmd")

    env = os.environ.copy()
    env["NODE_VIRTUAL_ENV"] = str(node_bin_dir.parent)
    env["PATH"] = str(node_bin_dir) + (os.pathsep + env["PATH"])

    subprocess.run(
        [str(npm_path), "install"],
        cwd=client_dir(),
        env=env,
        check=True,
    )
    subprocess.run(
        [str(npm_path), "run", "build"],
        cwd=client_dir(),
        env=env,
        check=True,
    )
