from __future__ import annotations

from importlib.metadata import version

from .op import CompressionMode
from .server import Viser4dServer

__version__ = version("viser4d")

__all__ = ["CompressionMode", "Viser4dServer", "__version__"]
