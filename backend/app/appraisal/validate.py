"""Validation of extracted records before review (KTD12, R13, R15)."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from app.appraisal.extract import RecordDraft, as_decimal

CRITICAL = {
    "transaction_price": ("price", "area", "transaction_date", "city"),
    "appraised_value": ("price", "area", "valuation_date", "city"),
    "asking_price": ("price", "area", "city"),
    "adjusted_comparable": ("price", "area", "city"),
}
CONFLICT_TOLERANCE = Decimal("0.005")


@dataclass
class Validation:
    computed_ppsqm: Decimal | None
    calc_definition: str | None
    conflict: bool
    missing_critical: list[str]
    status: str  # auto_extracted | needs_review


def compute_price_per_sqm(price: Decimal | None, area: Decimal | None) -> Decimal | None:
    if price is None or area is None or area <= 0:
        return None
    return (price / area).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class PriceChecks:
    computed: Decimal | None
    definition: str | None
    conflict: bool
    missing: list[str]


def price_checks(data_kind: str, price: Decimal | None, area: Decimal | None, stated: Decimal | None,
                 get) -> PriceChecks:
    """The one place that derives price/sqm, its lineage text, the stated-value conflict and missing
    critical fields. Used at extraction time and again after a human correction."""
    computed = compute_price_per_sqm(price, area)
    definition = f"price / area = {price} / {area}" if computed is not None else None
    conflict = bool(stated is not None and computed is not None and computed > 0
                    and abs(stated - computed) / computed > CONFLICT_TOLERANCE)
    missing = [f for f in CRITICAL.get(data_kind, ()) if get(f) is None]
    return PriceChecks(computed, definition, conflict, missing)


def validate(rec: RecordDraft) -> Validation:
    checks = price_checks(rec.data_kind, as_decimal(rec.get("price")), as_decimal(rec.get("area")),
                          as_decimal(rec.get("price_per_sqm_stated")), rec.get)
    computed, definition, conflict, missing = checks.computed, checks.definition, checks.conflict, checks.missing
    needs_review = bool(missing) or conflict or rec.ocr
    return Validation(computed, definition, conflict, missing, "needs_review" if needs_review else "auto_extracted")
