from __future__ import annotations

import viser as _viser

from .server import ViserServer

__all__ = ["ViserServer"]


def __getattr__(name: str):
    return getattr(_viser, name)


def __dir__():
    return sorted(set(__all__) | set(dir(_viser)))
