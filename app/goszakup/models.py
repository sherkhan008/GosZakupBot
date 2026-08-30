"""Dataclasses mirroring the parts of the GosZakup V3 GraphQL schema we use.

Field names follow the official schema (https://ows.goszakup.gov.kz/help/v3/schema/):
Lots, TrdBuy, PlnPoint (Lots.Plans), PlnPointsKato (PlnPoint.PlansKato), RefKato.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RefKato:
    code: Optional[str] = None
    name_ru: Optional[str] = None
    name_kz: Optional[str] = None
    full_name_ru: Optional[str] = None
    full_name_kz: Optional[str] = None


@dataclass
class PlanKato:
    full_delivery_place_name_ru: Optional[str] = None
    full_delivery_place_name_kz: Optional[str] = None
    ref_kato_code: Optional[str] = None
    ref_kato: Optional[RefKato] = None


@dataclass
class PlanPoint:
    desc_ru: Optional[str] = None
    desc_kz: Optional[str] = None
    extra_desc_ru: Optional[str] = None
    extra_desc_kz: Optional[str] = None
    kato_list: list[PlanKato] = field(default_factory=list)


@dataclass
class TrdBuy:
    id: Optional[int] = None
    number_anno: Optional[str] = None
    name_ru: Optional[str] = None
    name_kz: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    publish_date: Optional[str] = None
    ref_buy_status_id: Optional[int] = None


@dataclass
class Lot:
    id: int
    lot_number: Optional[str] = None
    trd_buy_number_anno: Optional[str] = None  # announcement/tender number -- NOT the lot number
    name_ru: Optional[str] = None
    name_kz: Optional[str] = None
    description_ru: Optional[str] = None
    description_kz: Optional[str] = None
    amount: Optional[float] = None
    last_update_date: Optional[str] = None
    index_date: Optional[str] = None
    trd_buy_id: Optional[int] = None
    trd_buy: Optional[TrdBuy] = None
    plans: list[PlanPoint] = field(default_factory=list)
