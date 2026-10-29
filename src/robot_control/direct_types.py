from __future__ import annotations

import math
from numbers import Real

ARMS = ("left", "right")
SPEEDS = ("slow", "medium", "fast")


def finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    return float(value)


def finite_vector(value: object, length: int, name: str) -> tuple[float, ...]:
    if isinstance(value, (str, bytes, dict)):
        raise ValueError(f"{name} must contain exactly {length} numbers")
    try:
        items = list(value)  # type: ignore[arg-type]
    except TypeError as exc:
        raise ValueError(f"{name} must contain exactly {length} numbers") from exc
    if len(items) != length:
        raise ValueError(f"{name} must contain exactly {length} numbers")
    return tuple(finite_number(item, name) for item in items)
