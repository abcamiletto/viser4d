from __future__ import annotations

import pathlib
import runpy

from setuptools import setup
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.egg_info import egg_info as _egg_info
from setuptools.command.sdist import sdist as _sdist


ROOT = pathlib.Path(__file__).resolve().parent
RUNTIME_PATH = ROOT / "src" / "viser4d" / "runtime.js"
_RUNTIME_READY = False
ensure_runtime_bundle = runpy.run_path(
    ROOT / "src" / "viser4d" / "_runtime_bundle.py"
)["ensure_runtime_bundle"]


def _ensure_runtime_bundle() -> None:
    global _RUNTIME_READY
    if _RUNTIME_READY:
        return
    ensure_runtime_bundle(RUNTIME_PATH)
    _RUNTIME_READY = True


class egg_info(_egg_info):
    def run(self) -> None:
        _ensure_runtime_bundle()
        super().run()


class sdist(_sdist):
    def run(self) -> None:
        _ensure_runtime_bundle()
        super().run()


class build_py(_build_py):
    def run(self) -> None:
        _ensure_runtime_bundle()
        super().run()


setup(
    cmdclass={
        "build_py": build_py,
        "egg_info": egg_info,
        "sdist": sdist,
    }
)
