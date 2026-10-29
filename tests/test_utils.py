"""Unit tests for shared validation and log-sanitizing helpers."""
import math

import pytest

from utils import is_finite_number, parse_int, single_line


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


def test_parse_int_accepts_integral_numbers_and_strings():
    assert parse_int(2) == 2
    assert parse_int(-4) == -4
    assert parse_int("3") == 3
    assert parse_int(2.0) == 2


@pytest.mark.parametrize("value", [True, False, 1.5, "1.5", math.inf, math.nan,
                                   10**400, None, [], {}])
def test_parse_int_rejects_non_integers_and_non_finite_values(value):
    with pytest.raises(ValueError):
        parse_int(value)


def test_single_line_removes_terminal_control_characters():
    assert single_line("safe\x1b[31mred\x07") == "safe [31mred"
