"""Unit tests for shared validation and log-sanitizing helpers."""
import math

from utils import is_finite_number, single_line


def test_is_finite_number_accepts_plain_numbers():
    assert is_finite_number(0) is True
    assert is_finite_number(-1.5) is True
    assert is_finite_number(10**100) is True


def test_is_finite_number_rejects_bools_strings_and_non_finite_values():
    for value in [True, False, "1", None, [], {}, math.nan, math.inf, -math.inf]:
        assert is_finite_number(value) is False


def test_is_finite_number_rejects_integers_too_large_for_a_float():
    assert is_finite_number(10**400) is False


def test_single_line_collapses_newlines_and_nuls():
    assert single_line("first\r\nsecond\nthird\x00fourth") == "first second thirdfourth"


def test_single_line_truncates():
    assert single_line("x" * 5000, limit=10) == "x" * 10
