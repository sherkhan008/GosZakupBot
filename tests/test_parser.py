from zoneinfo import ZoneInfo

from app.goszakup.models import Lot, PlanKato, PlanPoint, RefKato, TrdBuy
from app.goszakup.parser import (
    build_tender_url,
    derive_delivery_place,
    derive_display_name,
    parse_api_datetime,
    parse_lot,
)

TZ = ZoneInfo("Asia/Qyzylorda")


def test_build_tender_url_uses_announcement_with_lots_tab():
    assert build_tender_url(17447674) == "https://goszakup.gov.kz/ru/announce/index/17447674?tab=lots"


def test_build_tender_url_handles_missing_trd_buy_id():
    assert build_tender_url(None) == "https://goszakup.gov.kz/"


def test_parse_naive_datetime_uses_app_timezone():
    dt = parse_api_datetime("2026-08-12 14:00:00", TZ)
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.hour == 14
    assert dt.tzinfo.utcoffset(dt).total_seconds() == TZ.utcoffset(dt).total_seconds()


def test_parse_offset_datetime_is_respected_as_is():
    dt = parse_api_datetime("2026-08-12T14:00:00+05:00", TZ)
    assert dt is not None
    assert dt.utcoffset().total_seconds() == 5 * 3600


def test_parse_none_and_empty_returns_none():
    assert parse_api_datetime(None, TZ) is None
    assert parse_api_datetime("", TZ) is None


def test_derive_display_name_prefers_name_ru():
    lot = Lot(id=1, name_ru="Русское имя", name_kz="Қазақша атау")
    assert derive_display_name(lot) == "Русское имя"


def test_derive_display_name_falls_back_to_kz_then_trdbuy_then_default():
    assert derive_display_name(Lot(id=1, name_kz="Қазақша атау")) == "Қазақша атау"
    assert (
        derive_display_name(Lot(id=1, trd_buy=TrdBuy(name_ru="Конкурс")))
        == "Конкурс"
    )
    assert derive_display_name(Lot(id=1)) == "Без названия"


def test_derive_delivery_place_prefers_ru_and_dedupes():
    lot = Lot(
        id=1,
        plans=[
            PlanPoint(
                kato_list=[
                    PlanKato(full_delivery_place_name_ru="г. Астана"),
                    PlanKato(full_delivery_place_name_ru="г. Астана"),  # duplicate
                    PlanKato(full_delivery_place_name_kz="Алматы қ."),
                    PlanKato(ref_kato=RefKato(full_name_ru="г. Шымкент")),
                ]
            )
        ],
    )
    place = derive_delivery_place(lot)
    assert place == "г. Астана; Алматы қ.; г. Шымкент"


def test_derive_delivery_place_falls_back_to_not_specified():
    assert derive_delivery_place(Lot(id=1)) == "Не указано"


def test_parse_lot_handles_missing_nested_objects_gracefully():
    raw = {"id": 42, "nameRu": "Тест", "TrdBuy": None, "Plans": None}
    lot = parse_lot(raw)
    assert lot.id == 42
    assert lot.trd_buy is None
    assert lot.plans == []
