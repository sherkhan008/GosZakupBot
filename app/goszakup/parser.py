"""Parse raw GosZakup GraphQL JSON into models, and derive display fields.

Handles missing/null fields defensively -- the API is not guaranteed to
populate every field for every lot.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from app.goszakup.models import Lot, PlanKato, PlanPoint, RefKato, TrdBuy

logger = logging.getLogger(__name__)

# Public GosZakup portal shows lots under the "Лоты" tab of the announcement
# page; there is no reliable direct per-lot URL, so we link to the
# announcement and select that tab. Verified format:
# https://goszakup.gov.kz/ru/announce/index/<trdBuyId>?tab=lots
PUBLIC_ANNOUNCE_URL_TEMPLATE = "https://goszakup.gov.kz/ru/announce/index/{trd_buy_id}?tab=lots"


def build_tender_url(trd_buy_id: Optional[int]) -> str:
    if not trd_buy_id:
        return "https://goszakup.gov.kz/"
    return PUBLIC_ANNOUNCE_URL_TEMPLATE.format(trd_buy_id=trd_buy_id)


_NAIVE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
)


def parse_api_datetime(raw: Optional[str], app_timezone: ZoneInfo) -> Optional[datetime]:
    """Parse a GosZakup datetime string into a timezone-aware datetime.

    GosZakup V3 returns naive local datetimes (no offset, no 'Z'). Per spec,
    naive values are interpreted in APP_TIMEZONE. If an offset/'Z' is present
    (observed in some fields), it is respected instead.
    """
    if not raw:
        return None
    raw = raw.strip()
    if not raw:
        return None

    # Timezone-aware ISO 8601 (offset or 'Z')
    iso_candidate = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is not None:
            return dt
        # fromisoformat succeeded but produced a naive datetime -> localize below
    except ValueError:
        pass

    for fmt in _NAIVE_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            return dt.replace(tzinfo=app_timezone)
        except ValueError:
            continue

    logger.warning("Could not parse GosZakup datetime value: %r", raw)
    return None


def format_api_datetime(dt: datetime, app_timezone: ZoneInfo) -> str:
    """Format a timezone-aware datetime as a naive 'YYYY-MM-DD HH:MM:SS' string
    in APP_TIMEZONE, for use in GosZakup date-range filter values."""
    local_dt = dt.astimezone(app_timezone)
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")


def _parse_ref_kato(raw: Optional[dict[str, Any]]) -> Optional[RefKato]:
    if not raw:
        return None
    return RefKato(
        code=raw.get("code"),
        name_ru=raw.get("nameRu"),
        name_kz=raw.get("nameKz"),
        full_name_ru=raw.get("fullNameRu"),
        full_name_kz=raw.get("fullNameKz"),
    )


def _parse_plan_kato(raw: dict[str, Any]) -> PlanKato:
    return PlanKato(
        full_delivery_place_name_ru=raw.get("fullDeliveryPlaceNameRu"),
        full_delivery_place_name_kz=raw.get("fullDeliveryPlaceNameKz"),
        ref_kato_code=raw.get("refKatoCode"),
        ref_kato=_parse_ref_kato(raw.get("RefKato")),
    )


def _parse_plan_point(raw: dict[str, Any]) -> PlanPoint:
    kato_raw = raw.get("PlansKato") or []
    return PlanPoint(
        desc_ru=raw.get("descRu"),
        desc_kz=raw.get("descKz"),
        extra_desc_ru=raw.get("extraDescRu"),
        extra_desc_kz=raw.get("extraDescKz"),
        kato_list=[_parse_plan_kato(k) for k in kato_raw if k],
    )


def _parse_trd_buy(raw: Optional[dict[str, Any]]) -> Optional[TrdBuy]:
    if not raw:
        return None
    return TrdBuy(
        id=raw.get("id"),
        number_anno=raw.get("numberAnno"),
        name_ru=raw.get("nameRu"),
        name_kz=raw.get("nameKz"),
        start_date=raw.get("startDate"),
        end_date=raw.get("endDate"),
        publish_date=raw.get("publishDate"),
        ref_buy_status_id=raw.get("refBuyStatusId"),
    )


def parse_lot(raw: dict[str, Any]) -> Lot:
    plans_raw = raw.get("Plans") or []
    return Lot(
        id=raw["id"],
        lot_number=raw.get("lotNumber"),
        name_ru=raw.get("nameRu"),
        name_kz=raw.get("nameKz"),
        description_ru=raw.get("descriptionRu"),
        description_kz=raw.get("descriptionKz"),
        amount=raw.get("amount"),
        last_update_date=raw.get("lastUpdateDate"),
        index_date=raw.get("indexDate"),
        trd_buy_id=raw.get("trdBuyId"),
        trd_buy=_parse_trd_buy(raw.get("TrdBuy")),
        plans=[_parse_plan_point(p) for p in plans_raw if p],
    )


def candidate_text_fields(lot: Lot) -> list[Optional[str]]:
    """All text fields keyword matching should search, per spec section 7."""
    fields: list[Optional[str]] = [
        lot.name_ru,
        lot.name_kz,
        lot.description_ru,
        lot.description_kz,
    ]
    if lot.trd_buy:
        fields.append(lot.trd_buy.name_ru)
        fields.append(lot.trd_buy.name_kz)
    for plan in lot.plans:
        fields.append(plan.desc_ru)
        fields.append(plan.desc_kz)
        fields.append(plan.extra_desc_ru)
        fields.append(plan.extra_desc_kz)
    return fields


def derive_display_name(lot: Lot) -> str:
    if lot.name_ru:
        return lot.name_ru
    if lot.name_kz:
        return lot.name_kz
    if lot.trd_buy and lot.trd_buy.name_ru:
        return lot.trd_buy.name_ru
    if lot.trd_buy and lot.trd_buy.name_kz:
        return lot.trd_buy.name_kz
    return "Без названия"


def derive_delivery_place(lot: Lot) -> str:
    places: list[str] = []
    for plan in lot.plans:
        for kato in plan.kato_list:
            place = (
                kato.full_delivery_place_name_ru
                or kato.full_delivery_place_name_kz
                or (kato.ref_kato.full_name_ru if kato.ref_kato else None)
                or (kato.ref_kato.name_ru if kato.ref_kato else None)
            )
            if place and place not in places:
                places.append(place)
    if not places:
        return "Не указано"
    return "; ".join(places)
