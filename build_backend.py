from __future__ import annotations

import pathlib
import runpy

from setuptools import build_meta as _build_meta


ROOT = pathlib.Path(__file__).resolve().parent
RUNTIME_PATH = ROOT / "src" / "viser4d" / "runtime.js"
ensure_runtime_bundle = runpy.run_path(
    ROOT / "src" / "viser4d" / "_runtime_bundle.py"
)["ensure_runtime_bundle"]


def _ensure_runtime() -> None:
    ensure_runtime_bundle(RUNTIME_PATH)


def build_sdist(sdist_directory: str, config_settings=None) -> str:
    _ensure_runtime()
    return _build_meta.build_sdist(sdist_directory, config_settings)


def build_wheel(
    wheel_directory: str,
    config_settings=None,
    metadata_directory: str | None = None,
) -> str:
    _ensure_runtime()
    return _build_meta.build_wheel(
        wheel_directory,
        config_settings,
        metadata_directory,
    )


def build_editable(
    wheel_directory: str,
    config_settings=None,
    metadata_directory: str | None = None,
) -> str:
    _ensure_runtime()
    return _build_meta.build_editable(
        wheel_directory,
        config_settings,
        metadata_directory,
    )


def get_requires_for_build_sdist(config_settings=None) -> list[str]:
    return _build_meta.get_requires_for_build_sdist(config_settings)


def get_requires_for_build_wheel(config_settings=None) -> list[str]:
    return _build_meta.get_requires_for_build_wheel(config_settings)


def get_requires_for_build_editable(config_settings=None) -> list[str]:
    return _build_meta.get_requires_for_build_editable(config_settings)


def prepare_metadata_for_build_wheel(
    metadata_directory: str,
    config_settings=None,
) -> str:
    _ensure_runtime()
    return _build_meta.prepare_metadata_for_build_wheel(
        metadata_directory,
        config_settings,
    )


def prepare_metadata_for_build_editable(
    metadata_directory: str,
    config_settings=None,
) -> str:
    _ensure_runtime()
    return _build_meta.prepare_metadata_for_build_editable(
        metadata_directory,
        config_settings,
    )
