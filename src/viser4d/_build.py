"""Load the checked-in browser bundle, also shipped in wheels."""

import pathlib

RUNTIME_MARKER = "/*__VISER4D_RUNTIME__*/"


def runtime_source() -> str:
    bundle = pathlib.Path(__file__).with_name("runtime.js")
    return RUNTIME_MARKER + bundle.read_text()
