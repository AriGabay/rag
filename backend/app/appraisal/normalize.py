"""Parse Hebrew appraisal values into exact normalized forms. Originals are always kept by the
caller; a value that cannot be parsed returns None (unknown), never a guess (R12, R19)."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

from app.extraction.normalize_text import base_normalize

_NUM = re.compile(r"-?\d[\d,]*(?:\.\d+)?")
_MILLION = re.compile(r"(מ׳|מ'|מיליון|מליון)")
_HEB_MONTHS = {
    "ינואר": 1, "פברואר": 2, "מרץ": 3, "מרס": 3, "אפריל": 4, "מאי": 5, "יוני": 6, "יולי": 7,
    "אוגוסט": 8, "ספטמבר": 9, "אוקטובר": 10, "נובמבר": 11, "דצמבר": 12,
}
_AREA_TYPES = {
    "נטו": "net", "ברוטו": "gross", "רשום": "registered", "רשומה": "registered",
    "אקוויוולנטי": "equivalent", "אקויוולנטי": "equivalent", "אקוולנטי": "equivalent",
}


def parse_decimal(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    m = _NUM.search(raw.replace("‏", "").replace("‎", ""))
    if not m:
        return None
    token = m.group(0)
    # a comma followed by exactly three digits is a thousands separator; anything else is invalid
    if "," in token and not re.fullmatch(r"-?\d{1,3}(,\d{3})+(\.\d+)?", token):
        return None
    try:
        return Decimal(token.replace(",", ""))
    except InvalidOperation:
        return None


def parse_money(raw: str | None) -> Decimal | None:
    value = parse_decimal(raw)
    if value is None:
        return None
    if raw and _MILLION.search(raw) and value < 1000:
        value = value * Decimal(1_000_000)
    return value


def parse_date(raw: str | None) -> date | None:
    if not raw:
        return None
    text = raw.strip()
    m = re.search(r"(\d{1,2})[./-](\d{1,2})[./-](\d{2,4})", text)
    try:
        if m:
            day, month, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if year < 100:
                year += 2000 if year < 70 else 1900
            return date(year, month, day)
        m = re.search(r"([א-ת]+)\s+(\d{4})", text)
        if m and m.group(1) in _HEB_MONTHS:
            return date(int(m.group(2)), _HEB_MONTHS[m.group(1)], 1)
    except ValueError:
        return None
    return None


def parse_area_type(raw: str | None) -> str | None:
    if not raw:
        return None
    for word, code in _AREA_TYPES.items():
        if word in raw:
            return code
    return None


def parse_area(raw: str | None) -> tuple[Decimal | None, str | None]:
    return parse_decimal(raw), parse_area_type(raw)


def parse_block_parcel(raw: str | None) -> tuple[str | None, str | None, str | None]:
    if not raw:
        return None, None, None
    labeled = re.search(r"גוש\D*(\d+)\D+חלקה\D*(\d+)(?:\D+תת[ -]?חלקה\D*(\d+))?", raw)
    if labeled:
        return labeled.group(1), labeled.group(2), labeled.group(3)
    m = re.search(r"(\d+)\s*/\s*(\d+)(?:\s*/\s*(\d+))?", raw)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None, None, None


def normalize_place(raw: str | None) -> str | None:
    if not raw:
        return None
    value = base_normalize(raw).strip(" .,:;")
    return value or None


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))
