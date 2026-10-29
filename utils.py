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


def parse_int(value: Any, message: str = "must be an integer") -> int:
    """Return an integer, rejecting booleans, fractions, and non-finite values.

    A single strict parser avoids the inconsistent ``int(value)`` behavior
    scattered across request handlers and prevents ``OverflowError`` from
    reaching Flask as an HTML 500 response.
    """
    if isinstance(value, bool):
        raise ValueError(message)
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(message)
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(message) from exc
    if not is_finite_number(parsed):
        raise ValueError(message)
    return parsed


def single_line(value: Any, limit: int = 2000) -> str:
    """Collapse whitespace and remove control characters from untrusted text."""
    text = str(value).replace("\x00", "")
    text = "".join(char if char >= " " or char == "\t" else " " for char in text)
    return " ".join(text.split())[:limit]
