from __future__ import annotations

import math


def require_positive_float(name: str, value: float) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{name} must be a positive finite float, got {value!r}.")
    return number
