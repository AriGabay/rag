"""Hebrew unit parsing and conversion by dimension (U7, KTD8 step 4). No database."""

from decimal import Decimal

import pytest

from app.answering.units import (
    DimensionMismatch,
    convert,
    find_quantities,
    parse_mention_quantity,
    parse_unit,
)


@pytest.mark.parametrize(("quote", "value", "unit", "dimension"), [
    ("שטח הממ״ד 12 מ״ר", "12", "sqm", "area"),
    ('שטח הממ"ד 12 מ"ר', "12", "sqm", "area"),          # ASCII gershayim
    ("שטח הממ'ד 12 מ'ר", "12", "sqm", "area"),          # geresh written in place of gershayim
    ("12 מטר רבוע", "12", "sqm", "area"),
    ("12.5 מטרים רבועים", "12.5", "sqm", "area"),
    ("1200 ס״מ", "1200", "cm", "length"),
    ('גובה 2.70 מ\'', "2.70", "m", "length"),
    ("גובה תקרה 270 ס\"מ", "270", "cm", "length"),
    ("1,250,000 ₪", "1250000", "ILS", "currency"),
    ("₪ 1,250,000", "1250000", "ILS", "currency"),
    ("1.2 מיליון ש\"ח", "1.2", "million_ILS", "currency"),
    ("25,000 ₪ למ״ר", "25000", "ILS/sqm", "currency_per_area"),
    ("בנוי על 3 קומות", "3", "floor", "count"),
    ("בקומה 4", "4", "floor", "count"),
    ("12 יח'", "12", "dwelling_unit", "count"),
    ("10%", "10", "percent", "percent"),
    ("2 דונם", "2", "dunam", "area"),
])
def test_parse_value_and_unit_from_the_quote(quote, value, unit, dimension):
    q = parse_mention_quantity(quote, value)
    assert q is not None and q.value == Decimal(value)
    assert q.unit is not None and (q.unit.code, q.unit.dimension) == (unit, dimension)


@pytest.mark.parametrize(("quote", "value", "target", "expected"), [
    ("12 מ״ר", "12", "area", Decimal("12")),
    ("12 מ'ר", "12", "area", Decimal("12")),
    ("1200 ס״מ", "1200", "length", Decimal("12")),
    ("2 דונם", "2", "area", Decimal("2000")),
    ("1.2 מיליון ₪", "1.2", "currency", Decimal("1200000")),
    ("450 אלף ₪", "450", "currency", Decimal("450000")),
    ("3 קומות", "3", "count", Decimal("3")),
    ("30 שנה", "30", "duration", Decimal("360")),
])
def test_converts_to_the_canonical_unit_of_the_dimension(quote, value, target, expected):
    q = parse_mention_quantity(quote, value)
    conv = convert(q.value, q.unit, target)
    assert conv.value == expected and not conv.assumed


@pytest.mark.parametrize(("quote", "value", "target"), [
    ("1200 ס״מ", "1200", "area"),        # length is not area
    ("בקומה 3", "3", "area"),            # a floor number is not an area
    ("12 מ״ר", "12", "length"),
    ("10%", "10", "currency"),
    ("1,250,000 ₪", "1250000", "area"),
])
def test_cross_dimension_is_rejected(quote, value, target):
    q = parse_mention_quantity(quote, value)
    with pytest.raises(DimensionMismatch):
        convert(q.value, q.unit, target)


def test_bare_meters_for_an_area_is_an_assumption():
    q = parse_mention_quantity("ממ״ד של 12 מטר", "12")
    conv = convert(q.value, q.unit, "area")
    assert conv.value == Decimal("12") and conv.assumed


def test_unitless_number_requires_a_unitless_dimension():
    q = parse_mention_quantity("ממ״ד בשטח 12", "12")
    assert q is not None and q.unit is None
    with pytest.raises(DimensionMismatch):
        convert(q.value, q.unit, "area")
    assert convert(q.value, q.unit, "count").value == Decimal("12")


def test_adjacent_number_of_another_attribute_does_not_take_the_unit():
    """The floor number next to the safe-room area carries the floor's word, not the area unit."""
    quote = "הדירה בקומה 3, ממ״ד בשטח 12 מ״ר"
    q3 = parse_mention_quantity(quote, "3")
    assert q3.unit is not None and q3.unit.dimension == "count"
    with pytest.raises(DimensionMismatch):
        convert(q3.value, q3.unit, "area")
    q12 = parse_mention_quantity(quote, "12")
    assert convert(q12.value, q12.unit, "area").value == Decimal("12")


def test_value_must_occur_in_the_quote():
    assert parse_mention_quantity("ממ״ד בשטח 12 מ״ר", "14") is None
    assert parse_mention_quantity("ממ״ד בשטח 12 מ״ר", "שנים עשר") is None


def test_find_quantities_reads_every_number():
    found = find_quantities("מרפסת 8 מ״ר וממ״ד 12 מ״ר, קומה 2")
    assert [(q.value, q.unit.code if q.unit else None) for q in found] == [
        (Decimal("8"), "sqm"), (Decimal("12"), "sqm"), (Decimal("2"), "floor")]


def test_parse_unit_of_a_column_header():
    assert parse_unit("שטח ממ״ד (מ״ר)").code == "sqm"
    assert parse_unit("מ\"ר").code == "sqm"
    assert parse_unit("הערות") is None
