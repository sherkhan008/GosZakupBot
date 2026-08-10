from datetime import datetime, timedelta, timezone

from app.telegram.formatter import build_message, escape_html, format_amount


def test_format_amount_integer_grouped():
    assert format_amount(4850000) == "4 850 000 ₸"


def test_format_amount_none():
    assert format_amount(None) == "Не указано"


def test_format_amount_fractional_preserved():
    assert format_amount(4850000.5) == "4 850 000,50 ₸"


def test_escape_html_prevents_markup_injection():
    dangerous = "<script>alert('x')</script> & <b>bold</b>"
    escaped = escape_html(dangerous)
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped
    assert "&amp;" in escaped


def _sample_message() -> str:
    end_date = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    remaining = timedelta(hours=44, minutes=23)
    return build_message(
        name="Стеллаж металлический архивный",
        amount=4850000,
        delivery_place="г. Астана, район Алматы",
        end_date=end_date,
        remaining=remaining,
        url="https://goszakup.gov.kz/ru/announce/index/12345?tab=lots",
    )


def test_message_contains_only_the_six_required_fields():
    message = _sample_message()

    assert "Стеллаж металлический архивный" in message
    assert "4 850 000 ₸" in message
    assert "г. Астана, район Алматы" in message
    assert "12.08.2026 14:00" in message
    assert "44 ч 23 мин" in message
    assert "https://goszakup.gov.kz/ru/announce/index/12345?tab=lots" in message

    assert "📦" in message
    assert "💰" in message
    assert "📍" in message
    assert "Окончание" in message
    assert "Осталось" in message
    assert "🔗" in message


def test_message_excludes_forbidden_metadata():
    message = _sample_message().lower()

    forbidden_terms = [
        "заказчик",
        "организатор",
        "номер лота",
        "№ лота",
        "способ закупки",
        "статус",
        "ключевое слово",
        "категория",
        "описание",
        "еnstru",
        "энстру",
        "комментар",
    ]
    for term in forbidden_terms:
        assert term not in message, f"Forbidden term leaked into message: {term}"


def test_missing_amount_shows_fallback_text():
    end_date = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    message = build_message(
        name="Тест",
        amount=None,
        delivery_place="",
        end_date=end_date,
        remaining=timedelta(hours=10),
        url="https://goszakup.gov.kz/",
    )
    assert "Не указано" in message


def test_missing_name_uses_fallback():
    from app.telegram.formatter import NO_NAME

    end_date = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    message = build_message(
        name="",
        amount=1000,
        delivery_place="Алматы",
        end_date=end_date,
        remaining=timedelta(hours=10),
        url="https://goszakup.gov.kz/",
    )
    assert NO_NAME in message
