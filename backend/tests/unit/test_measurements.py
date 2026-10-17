"""Measurements keep their meaning: values, qualifiers in their own segment, tables expanded by column.

Synthetic sentences only (the office's reports never enter the repository). The model's output is given as
fixed objects, so these tests check the server's validation, not the model."""

from __future__ import annotations

from decimal import Decimal

from app.measurements.extract import (
    Annotation,
    Passage,
    TableAnnotation,
    TextMeasurement,
    expand_table,
    looks_like_identifier,
    validate_text,
)
from app.measurements.values import number_word, parse_amount

# --- values ------------------------------------------------------------------------------------------------


def test_written_values_parse_with_their_form():
    a = parse_amount("כ-21,000 ₪")
    assert (a.value, a.form) == (Decimal("21000"), "approximate")
    a = parse_amount("בגבולות של 9,500 ₪")
    assert (a.value, a.form) == (Decimal("9500"), "approximate")
    a = parse_amount("₪ 1,251,300")
    assert (a.value, a.form) == (Decimal("1251300"), "exact")
    a = parse_amount("65-85 ₪")
    assert (a.low, a.high, a.form) == (Decimal("65"), Decimal("85"), "range")
    assert parse_amount("(₪ 1,197,500)").value == Decimal("-1197500")
    assert parse_amount("6.70%").value == Decimal("6.70")
    assert parse_amount("החל מ- 11,000 ₪").form == "minimum"
    assert parse_amount("ללא ערך") is None


def test_a_date_is_not_read_as_a_range():
    assert parse_amount("14.12.2021").form == "exact"


def test_number_words():
    assert number_word("שישה") == Decimal(6)
    assert number_word("עשרים וחמישה") == Decimal(25)
    assert number_word("שתים עשרה") is None or number_word("שתים עשרה") == Decimal(12)
    assert number_word("בניין") is None


def test_identifier_numbers_are_not_measurements():
    assert looks_like_identifier("100495000", "other", "units")
    assert not looks_like_identifier("1,250", "count", "units")
    assert not looks_like_identifier("2019", "duration", "years")


# --- text statements ---------------------------------------------------------------------------------------

SENTENCE = ("בהתאם לסקר, ראוי לקבוע את השווי למ\"ר בנוי ברוטו למסחר בגבולות של 9,500 ₪, ללא מע\"מ "
            "ודמ\"ש ראויים למ\"ר בגבולות של 55 ₪ למ\"ר/חודש.")


def _passage(text: str, pid: str = "P1") -> dict[str, Passage]:
    return {pid: Passage(pid, "text", text, 7, None, "סיכום", False)}


def _m(**kw) -> TextMeasurement:
    base = dict(passage_id="P1", quote=SENTENCE, metric_quote="", metric="", metric_kind="other", value_text="",
                unit="ILS", period="none", period_quote="", area_basis="", vat="unknown", vat_quote="",
                subject="הנכס הנישום", subject_role="appraised_property", value_role="appraiser_determination",
                effective_date="")
    base.update(kw)
    return TextMeasurement(**base)


VALUE = dict(metric_quote='השווי למ"ר בנוי ברוטו למסחר', metric='שווי למ"ר בנוי ברוטו למסחר',
             metric_kind="value_per_area", value_text="9,500 ₪", unit="ILS_per_sqm", area_basis="בנוי ברוטו")
RENT = dict(metric_quote='דמ"ש ראויים למ"ר', metric='דמי שכירות ראויים למ"ר', metric_kind="rent_per_area",
            value_text="55 ₪", unit="ILS_per_sqm", period="month", period_quote='למ"ר/חודש')


def test_two_values_of_one_sentence_stay_apart_with_their_own_qualifiers():
    rows = validate_text([_m(**VALUE, vat="excluded", vat_quote='ללא מע"מ'), _m(**RENT)], _passage(SENTENCE))
    value, rent = rows
    assert (value.metric_kind, value.value, value.form, value.vat, value.area_basis) == (
        "value_per_area", Decimal("9500"), "approximate", "excluded", "בנוי ברוטו")
    assert value.status == "auto_validated"
    assert (rent.metric_kind, rent.value, rent.period, rent.vat) == ("rent_per_area", Decimal("55"), "month", "unknown")
    assert rent.status == "auto_validated"
    assert value.statement_key == rent.statement_key  # one statement, two measurements


def test_vat_written_for_the_value_is_not_given_to_the_rent():
    rows = validate_text([_m(**VALUE, vat="excluded", vat_quote='ללא מע"מ'),
                          _m(**RENT, vat="excluded", vat_quote='ללא מע"מ')], _passage(SENTENCE))
    rent = rows[1]
    assert rent.vat == "unknown"
    assert rent.status == "needs_review"
    assert any("מע״מ" in i for i in rent.issues)


def test_a_rent_without_its_period_goes_to_review():
    rows = validate_text([_m(**VALUE), _m(**{**RENT, "period": "unknown", "period_quote": ""})], _passage(SENTENCE))
    assert rows[1].status == "needs_review"
    assert any("חודשית" in i for i in rows[1].issues)


def test_floor_area_basis_is_kept_as_written_and_approximate_value_stays_approximate():
    text = "ראוי לקבוע את השווי למ\"ר בנוי בגבולות של כ-21,000 ₪ למ\"ר פלדלת כולל מע\"מ."
    m = TextMeasurement(passage_id="P1", quote=text, metric_quote='השווי למ"ר בנוי', metric='שווי למ"ר בנוי',
                        metric_kind="value_per_area", value_text="כ-21,000 ₪", unit="ILS_per_sqm", period="none",
                        period_quote="", area_basis="פלדלת", vat="included", vat_quote='כולל מע"מ',
                        subject="הנכס הנישום", subject_role="appraised_property",
                        value_role="appraiser_determination", effective_date="")
    (row,) = validate_text([m], _passage(text))
    assert (row.value, row.form, row.area_basis, row.vat) == (Decimal("21000"), "approximate", "פלדלת", "included")
    assert row.status == "auto_validated"


def test_an_area_basis_not_written_is_dropped():
    rows = validate_text([_m(**{**VALUE, "area_basis": "נטו"})], _passage(SENTENCE))
    assert rows[0].area_basis is None
    assert rows[0].status == "needs_review"


def test_a_quote_or_value_not_in_the_passage_is_not_kept():
    assert validate_text([_m(**VALUE, quote="משפט שלא נכתב")], _passage(SENTENCE)) == []
    assert validate_text([_m(**{**VALUE, "value_text": "9,900 ₪"})], _passage(SENTENCE)) == []


def test_a_value_read_from_an_uncertain_picture_goes_to_review():
    p = {"P1": Passage("P1", "text", SENTENCE, 7, None, "סיכום", True)}
    (row,) = validate_text([_m(**VALUE)], p)
    assert row.status == "needs_review"


# --- tables --------------------------------------------------------------------------------------------------

def _table(headers, rows, caption="להלן נתוני היצע למשרדים מהסביבה:", notes=()):
    structure = {"headers": headers, "rows": [{"cells": r} for r in rows], "caption": caption, "title": [],
                 "notes": list(notes), "section": "סקר"}
    header_text = "\n".join([caption, " | ".join(headers), *notes])
    return Passage("P2", "table", "", 9, 3, "סקר", False, table=structure, header_text=header_text)


def _ann(key, kind, unit, period="none", vat="unknown", quote="", skip=False):
    return Annotation(key=key, skip=skip, metric=key, metric_kind=kind, unit=unit, period=period, area_basis="",
                      vat=vat, qualifier_quote=quote)


def test_columns_table_expands_every_cell_with_its_column_meaning():
    p = _table(["כתובת", "שטח במ\"ר", "שכ\"ד חודשי", "שכ\"ד למ\"ר"],
               [["הגפן 3", "120", "₪ 6,600", "₪ 55"], ["הזית 7", "80", "₪ 4,880", "₪ 61"]])
    a = TableAnnotation(passage_id="P2", orientation="columns", subject_key="כתובת", subject_role="asking",
                        value_role="asking_price", annotations=[
                            _ann("כתובת", "other", "other", skip=True),
                            _ann("שטח במ\"ר", "area", "sqm"),
                            _ann("שכ\"ד חודשי", "rent", "ILS", period="month", quote="שכ\"ד חודשי"),
                            _ann("שכ\"ד למ\"ר", "rent_per_area", "ILS_per_sqm", period="month", quote="שכ\"ד חודשי"),
                        ])
    rows = expand_table(a, p)
    rent = [r for r in rows if r.metric_kind == "rent_per_area"]
    assert [(r.subject, r.value, r.period) for r in rent] == [("הגפן 3", Decimal("55"), "month"),
                                                             ("הזית 7", Decimal("61"), "month")]
    assert all(r.value_role == "asking_price" and r.table_index == 3 for r in rows)
    assert all(r.statement_key.startswith("t3:r0:") for r in rows if r.row_index == 0)
    assert len({r.statement_key for r in rows if r.row_index == 0}) == 3  # one key per cell
    assert not any(r.metric_kind == "other" for r in rows)


def test_table_vat_claim_without_header_words_is_dropped():
    p = _table(["כתובת", "מחיר"], [["הגפן 3", "₪ 1,500,000"]])
    a = TableAnnotation(passage_id="P2", orientation="columns", subject_key="כתובת", subject_role="asking",
                        value_role="asking_price", annotations=[_ann("מחיר", "price", "ILS", vat="included",
                                                                     quote="כולל מע\"מ")])
    (row,) = expand_table(a, p)
    assert row.vat == "unknown" and row.status == "needs_review"


def test_rows_table_reads_each_label_once():
    p = _table(["רכיב", "סכום"], [["שווי למ\"ר בנוי ברוטו (*)", "₪ 9,500"], ["סה\"כ שווי", "₪ 30,000,000"]],
               caption="תחשיב", notes=["(*) לפי שטחי שיווק"])
    a = TableAnnotation(passage_id="P2", orientation="rows", subject_key="", subject_role="appraised_property",
                        value_role="calculation", annotations=[
                            _ann("שווי למ\"ר בנוי ברוטו (*)", "value_per_area", "ILS_per_sqm"),
                            _ann("סה\"כ שווי", "value", "ILS")])
    rows = expand_table(a, p)
    assert [(r.metric_kind, r.value) for r in rows] == [("value_per_area", Decimal("9500")),
                                                       ("value", Decimal("30000000"))]


def test_a_value_outside_its_metric_words_gets_no_qualifiers():
    # the model names the rent's words for the 9,500 value: 9,500 is written before them, outside that stretch,
    # so the VAT words cannot be tied to it
    m = _m(**{**VALUE, "metric_quote": 'דמ"ש ראויים למ"ר', "vat": "excluded", "vat_quote": 'ללא מע"מ'})
    (row,) = validate_text([m], _passage(SENTENCE))
    assert row.vat == "unknown" and row.status == "needs_review"
