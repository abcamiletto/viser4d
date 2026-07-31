"""Client bundle loading and (editable-install) autobuild.

Wheels ship a prebuilt ``runtime.js``. Editable installs rebuild it with a
nodeenv-sandboxed esbuild when the TypeScript sources or protocol change.
``runtime_source()`` returns the bundle text tagged with ``RUNTIME_MARKER`` so it
can be recognized in exported recordings.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import warnings

RUNTIME_MARKER = "/*__VISER4D_RUNTIME__*/"

_SOURCE_SUFFIXES = (".ts", ".json", ".mjs")
_IGNORED_DIRS = {".nodeenv", "node_modules"}
_CODEGEN_INPUTS = ("_codegen.py", "_protocol.py")


def _package_dir() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent


def _client_dir() -> pathlib.Path:
    return _package_dir() / "client"


def _bundle_path() -> pathlib.Path:
    return _package_dir() / "runtime.js"


def runtime_source() -> str:
    ensure_client_is_built()
    try:
        return RUNTIME_MARKER + _bundle_path().read_text()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Missing generated client bundle at src/viser4d/runtime.js. "
            "viser4d could not rebuild it automatically."
        ) from exc


def ensure_client_is_built() -> None:
    """Ensure ``runtime.js`` exists and is fresh enough to load."""
    bundle = _bundle_path()
    client = _client_dir()
    has_sources = (client / "package.json").exists()
    if bundle.exists() and (
        not _is_editable_install() or not has_sources or _is_fresh()
    ):
        return
    try:
        _build_client()
    except (FileNotFoundError, RuntimeError, subprocess.CalledProcessError) as exc:
        if not bundle.exists():
            raise RuntimeError(
                "Missing generated client bundle at src/viser4d/runtime.js and "
                "viser4d could not rebuild it automatically."
            ) from exc
        warnings.warn(
            "viser4d client sources changed, but the bundle could not be rebuilt. "
            "Using the existing generated bundle.",
            stacklevel=2,
        )


def _is_editable_install() -> bool:
    package = _package_dir()
    return package.name == "viser4d" and package.parent.name == "src"


def _is_fresh() -> bool:
    package = _package_dir()
    newest_source = max(
        path.stat().st_mtime
        for path in _client_dir().rglob("*")
        if path.is_file()
        and path.suffix in _SOURCE_SUFFIXES
        and not any(part in _IGNORED_DIRS for part in path.parts)
    )
    newest_codegen = max((package / name).stat().st_mtime for name in _CODEGEN_INPUTS)
    return max(newest_source, newest_codegen) <= _bundle_path().stat().st_mtime


def _install_sandboxed_node() -> pathlib.Path:
    env_dir = _client_dir() / ".nodeenv"
    bin_dir = env_dir / ("Scripts" if sys.platform == "win32" else "bin")
    npx = (bin_dir / "npx").with_suffix(".cmd" if sys.platform == "win32" else "")
    if npx.exists():
        return bin_dir
    try:
        import nodeenv  # noqa: F401
    except ImportError:
        raise RuntimeError(
            "nodeenv is required to bootstrap Node.js. Install viser4d with its "
            "runtime dependencies."
        ) from None
    result = subprocess.run(
        [sys.executable, "-m", "nodeenv", "--node=24.12.0", str(env_dir)], check=False
    )
    if result.returncode != 0:
        raise RuntimeError("Failed to install Node.js using nodeenv.")
    return bin_dir


def _build_client() -> None:
    bin_dir = _install_sandboxed_node()
    npm = (bin_dir / "npm").with_suffix(".cmd" if sys.platform == "win32" else "")
    env = os.environ.copy()
    env["VISER4D_PYTHON"] = sys.executable
    env["NODE_VIRTUAL_ENV"] = str(bin_dir.parent)
    env["PATH"] = str(bin_dir) + os.pathsep + env["PATH"]
    subprocess.run([str(npm), "install"], cwd=_client_dir(), env=env, check=True)
    subprocess.run([str(npm), "run", "build"], cwd=_client_dir(), env=env, check=True)
