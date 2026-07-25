"""
Date-bound normalization tests.

The bug these exist for: `Timestamp` is a VARCHAR compared lexicographically, and `/` sorts
above `-`, so an ISO-8601 upper bound excluded every row and returned success. Any regression
here is silent by construction — an empty result, not an error — so the assertions below check
the *rewritten bound*, not just that a call succeeded.
"""
import pytest

from ml.dates import normalize_bound, normalize_prefix, normalize_range


@pytest.mark.parametrize("value", ["2022-09-05", "2022/09/05"])
def test_either_separator_accepted(value):
    assert normalize_bound(value) == ("2022/09/05 00:00", None)


def test_lower_bound_opens_the_day():
    assert normalize_bound("2022-09-05", upper=False)[0] == "2022/09/05 00:00"


def test_upper_bound_closes_the_day():
    """A bare upper bound must include the whole day, not stop at midnight — otherwise
    "up to the 5th" silently drops every transaction on the 5th."""
    assert normalize_bound("2022-09-05", upper=True)[0] == "2022/09/05 23:59"


@pytest.mark.parametrize("value,expected", [
    ("2022-09-05T14:30", "2022/09/05 14:30"),
    ("2022-09-05 14:30", "2022/09/05 14:30"),
    ("2022-09-05T14:30:59", "2022/09/05 14:30"),
    ("2022-09-05T14:30:59.123Z", "2022/09/05 14:30"),
])
def test_time_parts_are_preserved(value, expected):
    assert normalize_bound(value) == (expected, None)


def test_explicit_time_is_not_widened():
    assert normalize_bound("2022-09-05 08:00", upper=True)[0] == "2022/09/05 08:00"


@pytest.mark.parametrize("value", ["last tuesday", "", "09/05/2022", "2022-9-5", None, 20220905])
def test_unparseable_bounds_error(value):
    normalized, err = normalize_bound(value)
    assert normalized is None and err


@pytest.mark.parametrize("value", ["2022-13-01", "2022-09-00", "2022-09-05 25:00"])
def test_impossible_calendar_values_error(value):
    assert normalize_bound(value)[1]


def test_range_widens_both_ends_outward():
    pair, err = normalize_range(["2022-09-01", "2022-09-05"])
    assert err is None
    assert pair == ["2022/09/01 00:00", "2022/09/05 23:59"]


def test_range_ordering_is_enforced():
    assert normalize_range(["2022-09-05", "2022-09-01"])[1]


@pytest.mark.parametrize("value", [["2022-09-01"], "2022-09-01", None, []])
def test_range_shape_is_enforced(value):
    assert normalize_range(value)[1]


def test_normalized_bounds_sort_correctly_against_stored_values():
    """The property the whole module exists to restore: a stored timestamp inside the range
    must compare as inside it under plain string ordering."""
    low, high = normalize_range(["2022-09-01", "2022-09-05"])[0]
    assert low <= "2022/09/03 11:07" <= high
    assert not ("2022/09/06 00:01" <= high)


@pytest.mark.parametrize("value,expected", [
    ("2022-09", "2022/09"),
    ("2022", "2022"),
    ("2022-09-05", "2022/09/05"),
    ("ACH", "ACH"),
])
def test_prefix_normalization(value, expected):
    assert normalize_prefix(value) == (expected, None)
