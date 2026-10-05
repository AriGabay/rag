"""Rules-first fact extraction (KTD12) from stored pages and tables.

Report header lines ("label: value") give the subject property's appraised-value record and the
report context (city, neighborhood, valuation date, report date). Comparables tables mapped by
their Hebrew headers give one transaction record per row. Comparables without their own city or
neighborhood column inherit the report's values; the fact's source path marks that inheritance
so a reviewer can see it was taken from the report header, not from external knowledge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from app.appraisal.normalize import (
    normalize_place,
    property_type_code,
    vat_basis_code,
    parse_area,
    parse_area_type,
    parse_block_parcel,
    parse_date,
    parse_decimal,
    parse_money,
)
from app.extraction.normalize_text import base_normalize

HEADER_LABELS: dict[str, str] = {
    "עיר": "city",
    "שכונה": "neighborhood",
    "כתובת הנכס": "address",
    "סוג נכס": "property_type",
    "המועד הקובע": "valuation_date",
    "תאריך עריכת השומה": "report_date",
    "שטח הנכס": "area",
    "שווי הנכס": "price",
    "בסיס מע״מ": "vat_basis",
}
# Comparables-section labels: the document states the city/neighborhood of its comparables.
SECTION_LABELS: dict[str, str] = {
    "עיר העסקאות": "city",
    "שכונת העסקאות": "neighborhood",
}
# Ordered: more specific header phrases first.
COLUMN_VOCAB: list[tuple[str, str]] = [
    ("מחיר למ״ר", "price_per_sqm_stated"),
    ("מחיר למטר", "price_per_sqm_stated"),
    ("סוג שטח", "area_type"),
    ("סוג נכס", "property_type"),
    ("תאריך", "transaction_date"),
    ("גוש", "block_parcel"),
    ("כתובת", "address"),
    ("שכונה", "neighborhood"),
    ("עיר", "city"),
    ("חדרים", "rooms"),
    ("שטח", "area"),
    ("מחיר", "price"),
    ("שווי", "price"),
]


@dataclass
class FieldValue:
    original: str | None
    value: object  # normalized value (Decimal, date, str) or None
    source: dict = field(default_factory=dict)


@dataclass
class RecordDraft:
    data_kind: str
    page_no: int | None
    table_index: int | None
    row_index: int | None
    text_span: str | None
    ocr: bool
    fields: dict[str, FieldValue] = field(default_factory=dict)

    def get(self, name: str):
        fv = self.fields.get(name)
        return fv.value if fv else None


@dataclass
class ReportHeader:
    fields: dict[str, FieldValue]


_LABEL_LINE = re.compile(r"^\s*(?P<label>[^:]{2,30}?)\s*:\s*(?P<value>.+?)\s*$")


def parse_header(pages: list[tuple[int, str]], labels: dict[str, str] | None = None) -> ReportHeader:
    """Find "label: value" lines anywhere in the report (first occurrence wins)."""
    labels = HEADER_LABELS if labels is None else labels
    found: dict[str, FieldValue] = {}
    for page_no, page_text in pages:
        for line in page_text.splitlines():
            line_n = base_normalize(line)
            if labels is HEADER_LABELS and "גוש" in line_n and "חלקה" in line_n and "block_parcel" not in found:
                block, parcel, sub = parse_block_parcel(line_n)
                if block:
                    found["block_parcel"] = FieldValue(line.strip(), (block, parcel, sub), {"page": page_no})
                continue
            m = _LABEL_LINE.match(line_n)
            if not m:
                continue
            label = m.group("label").strip()
            key = labels.get(label)
            if key and key not in found:
                raw_value = line.split(":", 1)[1].strip() if ":" in line else m.group("value")
                found[key] = FieldValue(raw_value, None, {"page": page_no, "label": label})
    for key, fv in found.items():
        fv.value = _normalize_field(key, fv.original)
    if "area" in found:
        _, area_type = parse_area(found["area"].original)
        if area_type:
            found["area_type"] = FieldValue(found["area"].original, area_type, dict(found["area"].source))
    return ReportHeader(found)


def _normalize_field(key: str, raw):
    if key == "block_parcel":
        return raw
    if raw is None:
        return None
    if key == "property_type":
        return property_type_code(raw)
    if key == "vat_basis":
        return vat_basis_code(raw)
    if key in ("city", "neighborhood", "address"):
        return normalize_place(raw)
    if key in ("valuation_date", "report_date", "transaction_date"):
        return parse_date(raw)
    if key == "area":
        return parse_area(raw)[0]
    if key == "area_type":
        return parse_area_type(raw)
    if key in ("price", "price_per_sqm_stated"):
        return parse_money(raw)
    if key == "rooms":
        return parse_decimal(raw)
    return raw


def map_columns(headers: list[str]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for idx, header in enumerate(headers):
        h = base_normalize(header or "")
        for phrase, key in COLUMN_VOCAB:
            if phrase in h and key not in used:
                mapping[idx] = key
                used.add(key)
                break
    return mapping


def is_comparables_table(mapping: dict[int, str]) -> bool:
    keys = set(mapping.values())
    return "price" in keys and bool(keys & {"address", "block_parcel"})


def _comparables_context(header: ReportHeader, section: ReportHeader) -> dict[str, FieldValue]:
    """City/neighborhood come only from the comparables section's own labels (never guessed from the
    subject property); report dates and VAT basis come from the report header and are marked so."""
    ctx: dict[str, FieldValue] = {}
    for name in ("city", "neighborhood"):
        fv = section.fields.get(name)
        if fv and fv.value is not None:
            ctx[name] = fv
    for name in ("valuation_date", "report_date", "vat_basis"):
        fv = header.fields.get(name)
        if fv and fv.value is not None:
            ctx[name] = FieldValue(fv.original, fv.value, dict(fv.source) | {"inherited_from": "report_header"})
    return ctx


def extract_records(pages: list[tuple[int, str]], tables: list[dict], ocr_pages: set[int]) -> list[RecordDraft]:
    """``tables``: stored structures ({index, headers, rows:[{page, cells}], ocr})."""
    header = parse_header(pages)
    records: list[RecordDraft] = []

    # Subject property: appraised value from the report header.
    if "price" in header.fields:
        page = header.fields["price"].source.get("page")
        rec = RecordDraft("appraised_value", page, None, None, header.fields["price"].original, page in ocr_pages)
        for name in ("city", "neighborhood", "address", "property_type", "valuation_date", "report_date",
                     "area", "area_type", "price", "vat_basis"):
            if name in header.fields:
                rec.fields[name] = header.fields[name]
        if "block_parcel" in header.fields:
            _set_block_parcel(rec, header.fields["block_parcel"])
        records.append(rec)

    context = _comparables_context(header, parse_header(pages, SECTION_LABELS))
    for table in tables:
        mapping = map_columns(table["headers"])
        if not is_comparables_table(mapping):
            continue
        for row_index, row in enumerate(table["rows"]):
            cells = row["cells"]
            if not any((c or "").strip() for c in cells):
                continue
            page = row.get("page")
            rec = RecordDraft("transaction_price", page, table["index"], row_index, " | ".join(cells),
                              bool(table.get("ocr")) or (page in ocr_pages))
            for col, key in mapping.items():
                raw = cells[col] if col < len(cells) else None
                if raw is None or not str(raw).strip():
                    continue
                source = {"page": page, "table_index": table["index"], "row_index": row_index, "col": col}
                if key == "block_parcel":
                    _set_block_parcel(rec, FieldValue(raw, parse_block_parcel(raw), source))
                elif key == "area":
                    area, area_type = parse_area(raw)
                    rec.fields["area"] = FieldValue(raw, area, source)
                    if area_type and "area_type" not in rec.fields:
                        rec.fields["area_type"] = FieldValue(raw, area_type, source)
                else:
                    rec.fields[key] = FieldValue(raw, _normalize_field(key, raw), source)
            for name, fv in context.items():
                rec.fields.setdefault(name, fv)
            records.append(rec)
    return records


def _set_block_parcel(rec: RecordDraft, fv: FieldValue) -> None:
    block, parcel, sub = fv.value if isinstance(fv.value, tuple) else (None, None, None)
    for name, value in (("block", block), ("parcel", parcel), ("sub_parcel", sub)):
        if value:
            rec.fields[name] = FieldValue(fv.original, value, dict(fv.source))


def as_date(value) -> date | None:
    return value if isinstance(value, date) else None


def as_decimal(value) -> Decimal | None:
    return value if isinstance(value, Decimal) else None
