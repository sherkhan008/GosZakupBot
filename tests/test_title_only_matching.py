"""Final keyword-match decision must check ONLY Lots.nameRu / Lots.nameKz --
never descriptionRu/Kz, TrdBuy names, or Plan text.
"""
from __future__ import annotations

from app.filters.keyword_filter import KeywordFilter
from app.goszakup.models import Lot, PlanPoint, TrdBuy
from app.goszakup.parser import title_fields


def _kf() -> KeywordFilter:
    return KeywordFilter(["стеллаж", "сейф"])


def test_title_fields_returns_only_name_ru_and_name_kz():
    lot = Lot(
        id=1,
        name_ru="Стол",
        name_kz="Үстел",
        description_ru="Стеллаж металлический",
        description_kz="Металл стеллаж",
        trd_buy=TrdBuy(name_ru="Стеллаж конкурс", name_kz="Стеллаж"),
        plans=[PlanPoint(desc_ru="стеллаж", extra_desc_ru="стеллаж")],
    )
    assert title_fields(lot) == ["Стол", "Үстел"]


def test_match_true_when_keyword_in_name_ru():
    lot = Lot(id=1, name_ru="Стеллаж металлический архивный", name_kz=None)
    assert _kf().match_any(title_fields(lot)) == "стеллаж"


def test_match_true_when_keyword_in_name_kz():
    lot = Lot(id=1, name_ru=None, name_kz="Сейф кеңсе")
    assert _kf().match_any(title_fields(lot)) == "сейф"


def test_match_false_when_keyword_only_in_description_not_title():
    lot = Lot(
        id=1,
        name_ru="Офисная мебель разная",
        name_kz=None,
        description_ru="В комплекте: стеллаж металлический архивный",
    )
    assert _kf().match_any(title_fields(lot)) is None


def test_match_false_when_keyword_only_in_trd_buy_name():
    lot = Lot(
        id=1,
        name_ru="Офисная мебель разная",
        name_kz=None,
        trd_buy=TrdBuy(name_ru="Закупка стеллажей для архива"),
    )
    assert _kf().match_any(title_fields(lot)) is None


def test_match_false_when_keyword_only_in_plan_description():
    lot = Lot(
        id=1,
        name_ru="Офисная мебель разная",
        name_kz=None,
        plans=[PlanPoint(desc_ru="стеллаж", extra_desc_ru="сейф")],
    )
    assert _kf().match_any(title_fields(lot)) is None
