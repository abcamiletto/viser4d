"""Helpers for generating ``window.__viser4d_audio`` JavaScript calls."""

from __future__ import annotations

import json
from pathlib import Path

_NAMESPACE = "window.__viser4d_audio"

RUNTIME = (Path(__file__).parent / "runtime.js").read_text()


def call(method: str, *args: object) -> str:
    """Build a ``window.__viser4d_audio.<method>(...)`` JS call string."""
    serialized = ", ".join(json.dumps(a) for a in args)
    return f"{_NAMESPACE}.{method}({serialized});"
