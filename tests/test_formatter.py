from datetime import datetime, timedelta, timezone

from app.telegram.formatter import NUMBER_NOT_SPECIFIED, build_message, escape_html, format_amount


def test_format_amount_integer_grouped():
    assert format_amount(4850000) == "4 850 000 тг"


def test_format_amount_none():
    assert format_amount(None) == "Не указано"


def test_format_amount_fractional_preserved():
    assert format_amount(4850000.5) == "4 850 000,50 тг"


def test_escape_html_prevents_markup_injection():
    dangerous = "<script>alert('x')</script> & <b>bold</b>"
    escaped = escape_html(dangerous)
    assert "<script>" not in escaped
    assert "&lt;script&gt;" in escaped
    assert "&amp;" in escaped


def _sample_message(remaining: timedelta = timedelta(days=1, hours=6, minutes=15)) -> str:
    end_date = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    return build_message(
        name="Стеллаж металлический архивный",
        tender_number="17244806-1",
        lot_number="17244806-ОК2",
        amount=4850000,
        delivery_place="г. Астана, район Алматы",
        end_date=end_date,
        remaining=remaining,
        url="https://zakup.gov.kz/ru/search/announce?filter%5Bnumber%5D=17244806-1",
    )


def test_message_contains_all_required_fields():
    message = _sample_message()

    assert "Стеллаж металлический архивный" in message
    assert "17244806-1" in message
    assert "17244806-ОК2" in message
    assert "4 850 000 тг" in message
    assert "г. Астана, район Алматы" in message
    assert "12.08.2026 14:00" in message
    assert "1 күн 6 сағат 15 минут" in message
    assert "https://zakup.gov.kz/ru/search/announce?filter%5Bnumber%5D=17244806-1" in message

    assert "🆕" in message
    assert "📌 Атауы:" in message
    assert "🔢 Тендер нөмірі:" in message
    assert "📋 Лот нөмірі:" in message
    assert "💰 Сомасы:" in message
    assert "⏳ Аяқталу уақыты:" in message
    assert "⏱ Қалған уақыт:" in message
    assert "📍 Жеткізу орны:" in message
    assert "🔗 Тендерді ашу:" in message


def test_countdown_line_under_24_hours():
    message = _sample_message(remaining=timedelta(hours=6, minutes=15))
    countdown_line = next(line for line in message.splitlines() if "Қалған уақыт" in line)
    assert "6 сағат 15 минут" in countdown_line
    assert "күн" not in countdown_line


def test_countdown_line_under_1_hour():
    message = _sample_message(remaining=timedelta(minutes=45))
    countdown_line = next(line for line in message.splitlines() if "Қалған уақыт" in line)
    assert countdown_line.endswith("45 минут")


def test_countdown_line_never_negative():
    message = _sample_message(remaining=timedelta(hours=-5))
    countdown_line = next(line for line in message.splitlines() if "Қалған уақыт" in line)
    assert "-" not in countdown_line
    assert countdown_line.endswith("0 минут")


def test_tender_number_and_lot_number_are_distinct_and_not_confused():
    message = _sample_message()
    tender_line = next(line for line in message.splitlines() if "Тендер нөмірі" in line)
    lot_line = next(line for line in message.splitlines() if "Лот нөмірі" in line)
    assert "17244806-1" in tender_line
    assert "17244806-1" not in lot_line
    assert "17244806-ОК2" in lot_line


def test_message_excludes_forbidden_metadata():
    message = _sample_message().lower()

    forbidden_terms = [
        "заказчик",
        "организатор",
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


def test_missing_tender_number_shows_kazakh_fallback():
    end_date = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    message = build_message(
        name="Тест",
        tender_number=None,
        lot_number="LOT-1",
        amount=1000,
        delivery_place="Алматы",
        end_date=end_date,
        remaining=timedelta(hours=10),
        url="https://zakup.gov.kz/",
    )
    assert NUMBER_NOT_SPECIFIED in message
    tender_line = next(line for line in message.splitlines() if "Тендер нөмірі" in line)
    assert NUMBER_NOT_SPECIFIED in tender_line


def test_missing_lot_number_shows_kazakh_fallback():
    end_date = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    message = build_message(
        name="Тест",
        tender_number="17244806-1",
        lot_number=None,
        amount=1000,
        delivery_place="Алматы",
        end_date=end_date,
        remaining=timedelta(hours=10),
        url="https://zakup.gov.kz/",
    )
    lot_line = next(line for line in message.splitlines() if "Лот нөмірі" in line)
    assert NUMBER_NOT_SPECIFIED in lot_line


def test_missing_amount_shows_fallback_text():
    end_date = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    message = build_message(
        name="Тест",
        tender_number="1",
        lot_number="1",
        amount=None,
        delivery_place="",
        end_date=end_date,
        remaining=timedelta(hours=10),
        url="https://zakup.gov.kz/",
    )
    assert "Не указано" in message


def test_missing_name_uses_fallback():
    from app.telegram.formatter import NO_NAME

    end_date = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    message = build_message(
        name="",
        tender_number="1",
        lot_number="1",
        amount=1000,
        delivery_place="Алматы",
        end_date=end_date,
        remaining=timedelta(hours=10),
        url="https://zakup.gov.kz/",
    )
    assert NO_NAME in message


def test_tender_number_is_html_escaped():
    end_date = datetime(2026, 8, 12, 14, 0, tzinfo=timezone.utc)
    message = build_message(
        name="Тест",
        tender_number="<script>1</script>",
        lot_number="1",
        amount=1000,
        delivery_place="Алматы",
        end_date=end_date,
        remaining=timedelta(hours=10),
        url="https://zakup.gov.kz/",
    )
    assert "<script>" not in message
    assert "&lt;script&gt;" in message
