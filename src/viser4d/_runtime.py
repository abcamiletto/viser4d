from __future__ import annotations

import json
import pathlib

from viser import _messages

from . import _client_autobuild
from ._types import ClientRuntimeConfig, RuntimeMethod, RuntimePayload


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


def client_runtime_config_payload(
    *,
    num_steps: int,
    fps: float,
    base_fps: float,
    loop: bool,
    timeline_slider_uuid: str,
    timestep_sync_uuid: str,
) -> ClientRuntimeConfig:
    return ClientRuntimeConfig(
        numSteps=num_steps,
        fps=fps,
        baseFps=base_fps,
        loop=loop,
        timelineSliderUuid=timeline_slider_uuid,
        timestepSyncUuid=timestep_sync_uuid,
    )


def make_runtime_message(
    method: RuntimeMethod,
    payload: RuntimePayload,
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
