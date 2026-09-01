"""Builds the Telegram tender notification and formats values."""
from __future__ import annotations

import html
from datetime import datetime, timedelta
from typing import Optional

from app.filters.deadline_filter import format_remaining_kazakh

AMOUNT_NOT_SPECIFIED = "Не указано"
DELIVERY_NOT_SPECIFIED = "Не указано"
NO_NAME = "Без названия"
NUMBER_NOT_SPECIFIED = "Көрсетілмеген"
URL_NOT_SPECIFIED = "Сілтеме көрсетілмеген"


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
    return f"{sign}{grouped}{frac_text} тг"


def format_end_date(end_date: datetime) -> str:
    return end_date.strftime("%d.%m.%Y %H:%M")


def build_message(
    *,
    name: str,
    tender_number: Optional[str],
    lot_number: Optional[str],
    amount: Optional[float],
    delivery_place: str,
    end_date: datetime,
    remaining: timedelta,
    url: Optional[str],
) -> str:
    safe_name = escape_html(name or NO_NAME)
    safe_tender_number = escape_html(tender_number) if tender_number else NUMBER_NOT_SPECIFIED
    safe_lot_number = escape_html(lot_number) if lot_number else NUMBER_NOT_SPECIFIED
    safe_delivery = escape_html(delivery_place or DELIVERY_NOT_SPECIFIED)
    safe_url = escape_html(url) if url else URL_NOT_SPECIFIED
    amount_text = format_amount(amount)
    end_date_text = format_end_date(end_date)
    remaining_text = format_remaining_kazakh(remaining)

    return (
        "🆕 Жаңа тендер\n\n"
        f"📌 Атауы: {safe_name}\n"
        f"🔢 Тендер нөмірі: {safe_tender_number}\n"
        f"📋 Лот нөмірі: {safe_lot_number}\n"
        f"💰 Сомасы: {amount_text}\n"
        f"⏳ Аяқталу уақыты: {end_date_text}\n"
        f"⏱ Қалған уақыт: {remaining_text}\n"
        f"📍 Жеткізу орны: {safe_delivery}\n\n"
        f"🔗 Тендерді ашу:\n{safe_url}"
    )
