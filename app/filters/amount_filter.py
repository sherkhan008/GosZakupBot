"""Minimum-amount filter: amount must be STRICTLY greater than the configured
threshold (100000 KZT by default). Exactly the threshold does not pass.

A missing/unparseable amount does NOT pass -- we cannot confirm the minimum
is met, so it is treated conservatively as a rejection rather than silently
letting an unverifiable lot through.
"""
from __future__ import annotations

from typing import Optional


def passes_amount_filter(amount: Optional[float], min_amount: float) -> bool:
    if amount is None:
        return False
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return False
    return value > float(min_amount)
