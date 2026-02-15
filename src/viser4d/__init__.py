from __future__ import annotations

from importlib.metadata import version
import viser as _viser

from .op import CompressionMode
from .server import Viser4dServer

__version__ = version("viser4d")

__all__ = ["CompressionMode", "Viser4dServer", "__version__"]


def __getattr__(name: str):
    return getattr(_viser, name)


def __dir__():
    return sorted(set(__all__) | set(dir(_viser)))
