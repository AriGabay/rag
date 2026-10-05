"""Validated query conditions (KTD1). Parsers — rules or a model — may only produce this schema;
the SQL builder accepts nothing else. No free SQL ever reaches the database."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

DataKind = Literal["transaction_price", "appraised_value", "asking_price", "adjusted_comparable"]
DateField = Literal["transaction_date", "valuation_date", "report_date"]
Intent = Literal["calculation", "document_lookup", "explanation", "combined"]
AreaType = Literal["net", "gross", "registered", "equivalent", "other"]

DATA_KIND_LABELS = {
    "transaction_price": "מחירי עסקאות",
    "appraised_value": "שווי שנקבע בשומות",
    "asking_price": "מחירים מבוקשים",
    "adjusted_comparable": "נתוני השוואה מתואמים",
}
DATE_FIELD_LABELS = {
    "transaction_date": "תאריך העסקה",
    "valuation_date": "המועד הקובע של השומה",
    "report_date": "תאריך עריכת השומה",
}
AREA_TYPE_LABELS = {"net": "נטו", "gross": "ברוטו", "registered": "רשום", "equivalent": "אקוויוולנטי", "other": "אחר"}
PROPERTY_TYPE_LABELS = {
    "apartment": "דירה", "garden_apartment": "דירת גן", "penthouse": "פנטהאוז", "duplex": "דופלקס",
    "cottage": "קוטג׳", "house": "בית פרטי", "office": "משרד", "retail": "חנות", "land": "מגרש",
}
VAT_LABELS = {"included": "כולל מע״מ", "excluded": "לא כולל מע״מ"}


class QueryConditions(BaseModel):
    intent: Intent = "calculation"
    data_kind: DataKind | None = None
    date_field: DateField | None = None
    year_from: int | None = Field(default=None, ge=1950, le=2100)
    year_to: int | None = Field(default=None, ge=1950, le=2100)
    city: str | None = None
    neighborhood: str | None = None
    property_type: str | None = None
    area_type: AreaType | None = None
    vat_basis: Literal["included", "excluded"] | None = None
    aggregation: Literal["both", "mean", "weighted", "median"] = "both"

    @field_validator("year_to")
    @classmethod
    def _range(cls, v, info):
        start = info.data.get("year_from")
        if v is not None and start is not None and v < start:
            raise ValueError("year_to before year_from")
        return v

    def date_range(self) -> tuple[date, date] | None:
        """Explicit half-open range [Jan 1 of year_from, Jan 1 after year_to) (R19)."""
        if self.year_from is None:
            return None
        return date(self.year_from, 1, 1), date((self.year_to or self.year_from) + 1, 1, 1)

    def normalized_key(self) -> dict:
        return self.model_dump(exclude={"aggregation"})

    def describe(self) -> list[dict]:
        """Human-readable conditions for the answer card."""
        out = []
        if self.data_kind:
            out.append({"label": "סוג הנתון", "value": DATA_KIND_LABELS[self.data_kind]})
        if self.city:
            out.append({"label": "עיר", "value": self.city})
        if self.neighborhood:
            out.append({"label": "שכונה", "value": self.neighborhood})
        if self.year_from is not None and self.date_field:
            years = str(self.year_from) if self.year_to in (None, self.year_from) else f"{self.year_from}–{self.year_to}"
            start, end = self.date_range()
            out.append({"label": DATE_FIELD_LABELS[self.date_field],
                        "value": f"{years} (מ-{start:%d/%m/%Y} ועד לפני {end:%d/%m/%Y})"})
        if self.property_type:
            out.append({"label": "סוג נכס", "value": PROPERTY_TYPE_LABELS.get(self.property_type, self.property_type)})
        if self.area_type:
            out.append({"label": "בסיס שטח", "value": AREA_TYPE_LABELS[self.area_type]})
        if self.vat_basis:
            out.append({"label": "בסיס מע״מ", "value": VAT_LABELS[self.vat_basis]})
        return out
