"""Builds the exact 6-field Telegram notification and formats values.

The message must contain ONLY: name, amount, delivery location, deadline,
remaining time, and link -- nothing else (no customer, lot number, method,
status, matched keyword, etc.)
"""
from __future__ import annotations

import html
from datetime import datetime, timedelta
from typing import Optional

from app.filters.deadline_filter import format_remaining

AMOUNT_NOT_SPECIFIED = "Не указано"
DELIVERY_NOT_SPECIFIED = "Не указано"
NO_NAME = "Без названия"


def escape_html(text: str) -> str:
    return html.escape(text, quote=False)


def format_amount(amount: Optional[float]) -> str:
    if amount is None:
        return AMOUNT_NOT_SPECIFIED
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return AMOUNT_NOT_SPECIFIED

    sign = "-" if value < 0 else ""
    value = abs(value)
    int_part = int(value)
    frac = round((value - int_part) * 100)
    if frac >= 100:
        int_part += 1
        frac = 0

    grouped = f"{int_part:,}".replace(",", " ")
    frac_text = f",{frac:02d}" if frac else ""
    return f"{sign}{grouped}{frac_text} ₸"


def format_end_date(end_date: datetime) -> str:
    return end_date.strftime("%d.%m.%Y %H:%M")


def build_message(
    *,
    name: str,
    amount: Optional[float],
    delivery_place: str,
    end_date: datetime,
    remaining: timedelta,
    url: str,
) -> str:
    safe_name = escape_html(name or NO_NAME)
    safe_delivery = escape_html(delivery_place or DELIVERY_NOT_SPECIFIED)
    amount_text = format_amount(amount)
    end_date_text = format_end_date(end_date)
    remaining_text = format_remaining(remaining)

    return (
        f"\U0001f4e6 <b>{safe_name}</b>\n\n"
        f"\U0001f4b0 {amount_text}\n\n"
        f"\U0001f4cd {safe_delivery}\n\n"
        f"⏰ Окончание: {end_date_text}\n\n"
        f"⌛ Осталось: {remaining_text}\n\n"
        f"\U0001f517 {url}"
    )
