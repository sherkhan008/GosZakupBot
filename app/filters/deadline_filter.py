"""Deadline classification: pending / eligible-to-send / expired.

remaining_time = end_date - now (both timezone-aware).

Boundaries are inclusive:
    remaining_time >= MIN_HOURS_REMAINING  AND  remaining_time <= MAX_HOURS_REMAINING
        -> eligible to send

    remaining_time > MAX_HOURS_REMAINING
        -> still pending (kept for a later cycle, never sent yet)

    remaining_time < MIN_HOURS_REMAINING (including already past)
        -> expired (never sent)
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum


class DeadlineStatus(str, Enum):
    ELIGIBLE = "eligible"
    PENDING = "pending"
    EXPIRED = "expired"


def remaining_timedelta(end_date: datetime, now: datetime) -> timedelta:
    if end_date.tzinfo is None or now.tzinfo is None:
        raise ValueError("Both end_date and now must be timezone-aware")
    return end_date - now


def classify_deadline(
    remaining: timedelta, min_hours: int, max_hours: int
) -> DeadlineStatus:
    seconds = remaining.total_seconds()
    min_seconds = min_hours * 3600
    max_seconds = max_hours * 3600

    if seconds < min_seconds:
        return DeadlineStatus.EXPIRED
    if seconds <= max_seconds:
        return DeadlineStatus.ELIGIBLE
    return DeadlineStatus.PENDING


def format_remaining(remaining: timedelta) -> str:
    """Format as '44 ч 23 мин', using whole minutes (floored, never rounded up)."""
    total_seconds = max(0, int(remaining.total_seconds()))
    total_minutes = total_seconds // 60
    hours, minutes = divmod(total_minutes, 60)
    return f"{hours} ч {minutes} мин"
