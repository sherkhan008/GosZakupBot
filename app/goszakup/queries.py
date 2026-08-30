"""GraphQL query strings and filter builders for the GosZakup V3 API.

Field names verified against the official schema docs at
https://ows.goszakup.gov.kz/help/v3/schema/ (Lots, LotsFiltersInput, TrdBuy,
PlnPoint, PlnPointsKato, RefKato) as of 2026-08.
"""
from __future__ import annotations

from typing import Any, Optional

LOT_FIELDS = """
    id
    lotNumber
    trdBuyNumberAnno
    nameRu
    nameKz
    descriptionRu
    descriptionKz
    amount
    lastUpdateDate
    indexDate
    trdBuyId
    TrdBuy {
        id
        numberAnno
        nameRu
        nameKz
        startDate
        endDate
        publishDate
        refBuyStatusId
    }
    Plans {
        descRu
        descKz
        extraDescRu
        extraDescKz
        PlansKato {
            fullDeliveryPlaceNameRu
            fullDeliveryPlaceNameKz
            refKatoCode
            RefKato {
                nameRu
                nameKz
                fullNameRu
                fullNameKz
                code
            }
        }
    }
"""

LOTS_QUERY = f"""
query LotsQuery($filter: LotsFiltersInput, $limit: Int, $after: Int) {{
    Lots(filter: $filter, limit: $limit, after: $after) {{
        {LOT_FIELDS}
    }}
}}
"""


# Per the live V3 schema, nameDescriptionRu/nameDescriptionKz are `String`
# (a single value), while lastUpdateDate/indexDate are `[String]` (arrays,
# used as a 2-element [from, to] inclusive range). Passing a list into
# nameDescriptionRu/Kz fails server-side with:
#   "Expected type String at value.nameDescriptionRu; String cannot
#    represent an array value"
# These builders enforce that distinction so the mistake cannot recur.
NAME_DESCRIPTION_RU = "nameDescriptionRu"
NAME_DESCRIPTION_KZ = "nameDescriptionKz"


def build_single_keyword_filter(
    *,
    keyword: str,
    field: str,
    last_update_date_range: Optional[tuple[str, str]] = None,
) -> dict[str, Any]:
    """Build a LotsFiltersInput payload for ONE keyword against ONE search
    field (field must be NAME_DESCRIPTION_RU or NAME_DESCRIPTION_KZ).

    Used only by the bootstrap, one keyword/field/page at a time -- never
    pass a list of keywords here, the API rejects it.
    """
    if field not in (NAME_DESCRIPTION_RU, NAME_DESCRIPTION_KZ):
        raise ValueError(f"Unsupported keyword search field: {field}")
    if not isinstance(keyword, str):
        raise TypeError(f"{field} must receive a single string keyword, got {type(keyword)}")

    filt: dict[str, Any] = {field: keyword}
    if last_update_date_range:
        frm, to = last_update_date_range
        filt["lastUpdateDate"] = [frm, to]
    return filt


def build_date_range_filter(last_update_date_range: tuple[str, str]) -> dict[str, Any]:
    """Build a LotsFiltersInput payload that narrows candidates purely by the
    lastUpdateDate window (the array-valued [from, to] range filter). Used
    for incremental sync, where all 92 keywords are then matched locally
    against every lot returned instead of issuing 92 server-side searches.
    """
    frm, to = last_update_date_range
    return {"lastUpdateDate": [frm, to]}


def build_ids_filter(ids: list[int]) -> dict[str, Any]:
    return {"id": ids}
