from __future__ import annotations

import json
import pathlib
from typing import Any

from viser import _messages

from . import _client_autobuild


RUNTIME_MARKER = "/*__VISER4D_RUNTIME__*/"


def clamp(value: int, lower: int, upper: int) -> int:
    return max(lower, min(value, upper))


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


def runtime_config_payload(
    *,
    num_steps: int,
    fps: float,
    base_fps: float,
    loop: bool,
    timestep_sync_uuid: str | None,
) -> dict[str, Any]:
    return {
        "numSteps": num_steps,
        "fps": fps,
        "baseFps": base_fps,
        "loop": loop,
        "timestepSyncUuid": timestep_sync_uuid,
    }


def make_runtime_message(
    method: str,
    payload: dict[str, Any],
) -> _messages.RunJavascriptMessage:
    source = (
        RUNTIME_MARKER
        + f"""
(() => {{
  const invoke = () => {{
    if (!window.__VISER4D__) return false;
    window.__VISER4D__.{method}({json.dumps(payload)});
    return true;
  }};
  if (invoke()) return;
  const timer = window.setInterval(() => {{
    if (!invoke()) return;
    window.clearInterval(timer);
  }}, 50);
}})();
"""
    )
    return _messages.RunJavascriptMessage(source)
