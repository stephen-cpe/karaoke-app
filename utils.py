"""Small validation and text helpers shared by the app and session state."""
from __future__ import annotations

import math
from typing import Any


def is_finite_number(value: Any) -> bool:
    """Return True only for finite int/float values (booleans excluded).

    ``math.isfinite`` raises ``OverflowError`` for integers too large to
    convert to a float, so treat those as invalid instead of returning a 500.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def single_line(value: Any, limit: int = 2000) -> str:
    """Collapse whitespace and remove NULs from untrusted log text."""
    text = str(value).replace("\x00", "")
    return " ".join(text.split())[:limit]
