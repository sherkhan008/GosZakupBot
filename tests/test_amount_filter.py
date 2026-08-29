"""amount > 100000 KZT; exactly the threshold does not pass."""
from __future__ import annotations

import pytest

from app.filters.amount_filter import passes_amount_filter

MIN_AMOUNT = 100000


@pytest.mark.parametrize(
    "amount,expected",
    [
        (99999, False),
        (100000, False),  # exactly the threshold must NOT pass
        (100000.0, False),
        (100001, True),
        (100001.0, True),
        (1_000_000, True),
        (0, False),
        (-5, False),
    ],
)
def test_amount_boundaries(amount, expected):
    assert passes_amount_filter(amount, MIN_AMOUNT) is expected


def test_amount_none_does_not_pass():
    assert passes_amount_filter(None, MIN_AMOUNT) is False


def test_amount_as_numeric_string_is_coerced():
    assert passes_amount_filter("150000", MIN_AMOUNT) is True
    assert passes_amount_filter("100000", MIN_AMOUNT) is False


def test_amount_unparseable_string_does_not_pass():
    assert passes_amount_filter("not-a-number", MIN_AMOUNT) is False


def test_amount_respects_configured_threshold():
    assert passes_amount_filter(500000, min_amount=1_000_000) is False
    assert passes_amount_filter(1_500_000, min_amount=1_000_000) is True
