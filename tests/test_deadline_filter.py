from datetime import datetime, timedelta, timezone

import pytest

from app.filters.deadline_filter import (
    DeadlineStatus,
    classify_deadline,
    format_remaining,
    remaining_timedelta,
)

MIN_HOURS = 5
MAX_HOURS = 72


@pytest.mark.parametrize(
    "delta,expected",
    [
        (timedelta(hours=4, minutes=59), DeadlineStatus.EXPIRED),
        (timedelta(hours=5, minutes=0), DeadlineStatus.ELIGIBLE),
        (timedelta(hours=24), DeadlineStatus.ELIGIBLE),
        (timedelta(hours=72), DeadlineStatus.ELIGIBLE),
        (timedelta(hours=72, minutes=1), DeadlineStatus.PENDING),
        (timedelta(hours=100), DeadlineStatus.PENDING),
        (timedelta(hours=-1), DeadlineStatus.EXPIRED),
    ],
)
def test_classify_deadline_boundaries(delta: timedelta, expected: DeadlineStatus):
    assert classify_deadline(delta, MIN_HOURS, MAX_HOURS) == expected


def test_remaining_timedelta_requires_timezone_aware():
    aware = datetime(2026, 1, 1, tzinfo=timezone.utc)
    naive = datetime(2026, 1, 1)
    with pytest.raises(ValueError):
        remaining_timedelta(aware, naive)
    with pytest.raises(ValueError):
        remaining_timedelta(naive, aware)


def test_remaining_timedelta_computes_correct_delta():
    now = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    end = now + timedelta(hours=44, minutes=23)
    assert remaining_timedelta(end, now) == timedelta(hours=44, minutes=23)


def test_format_remaining_matches_russian_format():
    assert format_remaining(timedelta(hours=44, minutes=23)) == "44 ч 23 мин"
    assert format_remaining(timedelta(hours=5, minutes=0)) == "5 ч 0 мин"


def test_format_remaining_floors_seconds_without_inflating_minutes():
    # 5 minutes and 59 seconds must show as 5 minutes, never rounded up to 6.
    assert format_remaining(timedelta(minutes=5, seconds=59)) == "0 ч 5 мин"


def test_format_remaining_clamps_negative_to_zero():
    assert format_remaining(timedelta(hours=-2)) == "0 ч 0 мин"


def test_pending_then_eligible_transition():
    # A tender first seen with 100 hours remaining is 'pending'...
    status_first = classify_deadline(timedelta(hours=100), MIN_HOURS, MAX_HOURS)
    assert status_first == DeadlineStatus.PENDING
    # ...and later, once only 70 hours remain, becomes eligible to send.
    status_later = classify_deadline(timedelta(hours=70), MIN_HOURS, MAX_HOURS)
    assert status_later == DeadlineStatus.ELIGIBLE


def test_never_sent_and_now_under_5_hours_is_expired():
    status = classify_deadline(timedelta(hours=4), MIN_HOURS, MAX_HOURS)
    assert status == DeadlineStatus.EXPIRED
