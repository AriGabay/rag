"""Hebrew unit parsing and conversion by dimension (U7, KTD8 step 4). No database."""

from decimal import Decimal

import pytest

from app.answering.units import (
    DimensionMismatch,
    area_from_dimensions,
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


# --- counts written as words, "none" as zero, and W×H dimensions (real-model sample, category c) ----------

@pytest.mark.parametrize(("quote", "value", "expected"), [
    ("בדירה שתי מרפסות", "2", Decimal("2")),
    ("בדירה שתי מרפסות", "שתי", Decimal("2")),
    ("שתי חניות תת-קרקעיות", "2", Decimal("2")),
    ("מקום חניה אחד בחניון הבניין", "1", Decimal("1")),
    ("מרפסת סלון אחת בשטח 14 מ״ר", "1", Decimal("1")),
    ("ושלושה חדרי שירות", "3", Decimal("3")),
    ("שתים עשרה יחידות", "12", Decimal("12")),
    ("חמישה עשר מקומות", "15", Decimal("15")),
    ("עשרים מחסנים", "20", Decimal("20")),
    ("לדירה אין חניה צמודה", "0", Decimal("0")),
    ("ללא מעלית", "אין", Decimal("0")),
])
def test_count_words_are_read_as_numbers(quote, value, expected):
    q = parse_mention_quantity(quote, value, "count")
    assert q is not None and q.value == expected
    assert convert(q.value, q.unit, "count").value == expected


def test_number_words_count_only_for_a_count_dimension():
    assert parse_mention_quantity("בדירה שתי מרפסות", "2", "area") is None
    assert parse_mention_quantity("בדירה שתי מרפסות", "2") is None
    assert parse_mention_quantity("לדירה אין חניה", "0", "area") is None
    # the word must be the stated number: "שתי" is not three, and a word inside another word is not a number
    assert parse_mention_quantity("בדירה שתי מרפסות", "3", "count") is None
    assert parse_mention_quantity("השתיים", "2", "count") is not None
    assert parse_mention_quantity("משתייה", "2", "count") is None


@pytest.mark.parametrize(("quote", "value", "expected"), [
    ("מרחב מוגן במידות פנים של 300 על 350 ס״מ", "300 על 350", Decimal("10.5")),
    ("מידות החדר 250×300 ס״מ", "250×300", Decimal("7.5")),
    ("מידות 2.5 x 3 מ׳", "2.5", Decimal("7.5")),
    ("מידות 2.5 מ׳ על 3 מ׳", "7.5", Decimal("7.5")),
    ("מידות 250 ס״מ X 300 ס״מ", "300", Decimal("7.5")),
])
def test_two_lengths_give_an_assumed_area(quote, value, expected):
    conv = area_from_dimensions(quote, value)
    assert conv is not None and conv.value == expected and conv.canonical_unit == "sqm" and conv.assumed


@pytest.mark.parametrize(("quote", "value"), [
    ("מידות 300 על 350", "300"),                        # no length unit: nothing says these are lengths
    ("300 על 350 מ״ר", "300"),                           # an area unit is not a length
    ("חדר 3 על 4 מ׳ וחדר 2 על 3 מ׳", "3"),               # two pairs: which one is ambiguous
    ("מידות 300 על 350 ס״מ", "400"),                     # the stated value is neither length nor the product
    ("בנוי על 3 קומות", "3"),                            # "על" without two lengths
])
def test_dimensions_without_two_explicit_lengths_give_nothing(quote, value):
    assert area_from_dimensions(quote, value) is None
