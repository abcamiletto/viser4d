import pathlib

from . import _client_autobuild


RUNTIME_MARKER = "/*__VISER4D_RUNTIME__*/"


def runtime_source() -> str:
    runtime_path = pathlib.Path(__file__).resolve().parent / "runtime.js"
    _client_autobuild.ensure_client_is_built()
    try:
        return RUNTIME_MARKER + runtime_path.read_text()
    except FileNotFoundError as exc:
        raise RuntimeError(
            "Missing generated client bundle at src/viser4d/runtime.js. "
            "viser4d could not rebuild it automatically."
        ) from exc
