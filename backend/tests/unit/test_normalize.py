from datetime import date
from decimal import Decimal

import pytest

from app.appraisal.normalize import (
    normalize_place,
    parse_area,
    parse_area_type,
    parse_block_parcel,
    parse_date,
    parse_decimal,
    parse_money,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,250,000", Decimal("1250000")),
        ("₪1,250,000", Decimal("1250000")),
        ("1,250,000 ₪", Decimal("1250000")),
        ("1.25 מ׳ ₪", Decimal("1250000")),
        ("1.25 מיליון ש\"ח", Decimal("1250000")),
        ("2,500,000.50", Decimal("2500000.50")),
        ("26,315.79", Decimal("26315.79")),
    ],
)
def test_parse_money(raw, expected):
    assert parse_money(raw) == expected


def test_parse_decimal_keeps_decimals_and_rejects_garbage():
    assert parse_decimal("95.5") == Decimal("95.5")
    assert parse_decimal("abc") is None
    assert parse_decimal("") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("15/03/2024", date(2024, 3, 15)),
        ("15.03.2024", date(2024, 3, 15)),
        ("15.03.24", date(2024, 3, 15)),
        ("5-1-2023", date(2023, 1, 5)),
        ("מרץ 2024", date(2024, 3, 1)),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected


def test_impossible_date_is_unknown():
    assert parse_date("31/02/2024") is None
    assert parse_date("לא ידוע") is None


def test_parse_area_and_type():
    assert parse_area("95 מ״ר נטו") == (Decimal("95"), "net")
    assert parse_area('110.5 מ"ר ברוטו') == (Decimal("110.5"), "gross")
    assert parse_area("80") == (Decimal("80"), None)
    assert parse_area_type("אקוויוולנטי") == "equivalent"
    assert parse_area_type("רשום") == "registered"


def test_parse_block_parcel():
    assert parse_block_parcel("6158/42") == ("6158", "42", None)
    assert parse_block_parcel("6158/42/7") == ("6158", "42", "7")
    assert parse_block_parcel("גוש: 6158 חלקה: 42 תת חלקה: 7") == ("6158", "42", "7")
    assert parse_block_parcel("-") == (None, None, None)


def test_normalize_place():
    assert normalize_place("  רמת  גן ") == "רמת גן"
    assert normalize_place('ר"ג') == "ר״ג"
