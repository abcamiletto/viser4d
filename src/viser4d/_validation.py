from __future__ import annotations

import math
import os
import re


_BYTE_UNITS = {
    "": 1,
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
    "KIB": 1 << 10,
    "MIB": 1 << 20,
    "GIB": 1 << 30,
    "TIB": 1 << 40,
}
_BYTE_SIZE_PATTERN = re.compile(r"^\s*(\d+)\s*([A-Za-z]*)\s*$")


def require_positive_float(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} must be a positive finite float, got {value!r}.")
    return number


def env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}.") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}.")
    return value


def env_byte_size(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    match = _BYTE_SIZE_PATTERN.fullmatch(raw)
    if match is None:
        raise ValueError(
            f"{name} must be an integer byte count or a size like '512MB' or '1GiB', "
            f"got {raw!r}."
        )
    unit = match.group(2).upper()
    multiplier = _BYTE_UNITS.get(unit)
    if multiplier is None:
        raise ValueError(
            f"{name} uses an unsupported size unit {unit!r}; supported units are "
            "'B', 'KB', 'MB', 'GB', 'TB', 'KiB', 'MiB', 'GiB', and 'TiB'."
        )
    value = int(match.group(1)) * multiplier
    if value < 1:
        raise ValueError(f"{name} must be a positive integer, got {raw!r}.")
    return value
