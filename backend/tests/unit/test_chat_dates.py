"""The pure side of the datum-date resolution (round 11 U6b: R5, R6, R7; KTD3, KTD4): which dates the label vocabulary
finds in stored blocks, how a unit's valuation date is settled, the `as_of` order (own date, table range, unit
valuation), the forecast rule and the measurement's stored date. No database. All texts are synthetic."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.chat import dates as D

ANCHOR = date(2026, 10, 14)


def block(i: int, text: str, kind: str = "paragraph"):
    return SimpleNamespace(block_index=i, kind=kind, text=text)


def found(blocks):
    return [(f.block_index, f.iso, f.precision) for f in D.derive(blocks)]


def stated(i: int, iso: str, precision: str = "day", unit_no: int | None = 1) -> D.StatedDate:
    return D.StatedDate(i, iso, precision, iso, "stated", unit_no)


# --- derivation: the labelled valuation date, never the report-writing or signature date -------------------------

def test_the_valuation_date_is_taken_and_the_report_date_never_is():
    blocks = [block(0, "המועד הקובע: 15/04/2024"), block(1, "תאריך עריכת השומה: 22/04/2025")]
    assert found(blocks) == [(0, "2024-04-15", "day")]


@pytest.mark.parametrize("text, iso, precision", [
    ("המועד הקובע: 15/04/2024", "2024-04-15", "day"),
    ("המועד הקובע לשומה הוא 15.4.2024.", "2024-04-15", "day"),
    ("שווי הנכס למועד הקובע ליום 15 באפריל 2024 הינו כמפורט להלן.", "2024-04-15", "day"),
    ("מועד קובע – אפריל 2024", "2024-04", "month"),
    ("המועד הקובע לשומה ליום 2024-04-15", "2024-04-15", "day"),
    ("המועד הקובע | 15/04/2024", "2024-04-15", "day"),
    ("המועד הקובע\n15/04/2024", "2024-04-15", "day"),
])
def test_a_valuation_label_takes_the_date_that_follows_it_with_its_precision(text, iso, precision):
    assert found([block(3, text)]) == [(3, iso, precision)]


def test_a_label_alone_in_a_cell_takes_the_date_of_the_next_cell():
    assert found([block(4, "המועד הקובע", "table"), block(5, "15/04/2024", "table")]) == [(5, "2024-04-15", "day")]


@pytest.mark.parametrize("text", [
    "תאריך עריכת השומה: 22/04/2025",
    "תאריך הדוח: 22/04/2025",
    "נחתם ביום 22/04/2025",
    "חתימה: ____ תאריך חתימה 22/04/2025",
    "בתאריך 15/04/2024 נערכה בדיקה במקום",               # a date with no valuation label
    "המועד הקובע: 15/04/24",                                # a two-digit year is no supported date
    "המועד הקובע הוא תשפ״ד",                                # a Hebrew-calendar year is unknown
    "המועד הקובע הרבעון הראשון Q1 2024",
    "המועד הקובע יקבע בהמשך. עסקה מיום 15/04/2024.",        # the date is not the label's
    "המועד הקובע בין 2020 ל-2022",                           # a range is no valuation date
])
def test_what_is_not_a_labelled_valuation_date_is_not_stored(text):
    assert found([block(0, text)]) == []


def test_a_report_date_beside_the_valuation_date_in_one_block_is_not_taken_as_it():
    blocks = [block(0, "תאריך עריכת השומה: 22/04/2025, המועד הקובע: 15/04/2024")]
    assert found(blocks) == [(0, "2024-04-15", "day")]
    assert [p.iso for p in D.report_dates(blocks[0].text)] == ["2025-04-22"]


def test_the_report_date_is_recognised_for_both_report_and_signature_labels():
    assert [p.iso for p in D.report_dates("נחתם ביום 03/05/2025")] == ["2025-05-03"]
    assert [p.iso for p in D.report_dates("תאריך הדוח: 03.05.2025")] == ["2025-05-03"]
    assert D.report_dates("המועד הקובע: 15/04/2024") == []


def test_a_picture_block_and_bidi_marks_are_handled():
    assert found([block(0, "המועד הקובע: 15/04/2024", "image")]) == []
    assert found([block(1, "‏המועד הקובע‏: ‎15/04/2024")]) == [(1, "2024-04-15", "day")]


def test_the_same_date_twice_in_one_block_is_one_find():
    assert found([block(0, "המועד הקובע: 15/04/2024. שוב, המועד הקובע: 15/04/2024")]) == [(0, "2024-04-15", "day")]


# --- the unit's valuation date ------------------------------------------------------------------------------------

def test_one_valuation_date_in_a_unit_is_stated():
    uv = D.resolve_unit([stated(2, "2024-04-15")], 1)
    assert (uv.status, uv.date.iso, uv.unit_no) == ("stated", "2024-04-15", 1)


def test_two_differing_dates_in_a_unit_are_a_conflict_with_no_date():
    uv = D.resolve_unit([stated(2, "2024-04-15"), stated(9, "2025-01-01")], 1)
    assert (uv.status, uv.date) == ("conflict", None) and len(uv.dates) == 2


def test_the_same_date_repeated_is_not_a_conflict_and_a_month_inside_a_day_agrees_with_it():
    assert D.resolve_unit([stated(2, "2024-04-15"), stated(9, "2024-04-15")], 1).status == "stated"
    uv = D.resolve_unit([stated(2, "2024-04", "month"), stated(9, "2024-04-15")], 1)
    assert (uv.status, uv.date.iso) == ("stated", "2024-04-15")
    assert D.resolve_unit([stated(2, "2024-04", "month"), stated(9, "2024-05-15")], 1).status == "conflict"


def test_no_dates_is_none():
    uv = D.resolve_unit([], 1)
    assert (uv.status, uv.date, uv.dates) == ("none", None, ())


def test_assignment_follows_the_units_as_they_are_now():
    from app.chat.units import Unit

    def unit(no, first, last):
        return Unit(no, "appraisal", None, (), {}, None, None, first, last, "certain", {})

    found_ = [D.Found(3, "2024-04-15", "day", "15/04/2024"), D.Found(40, "2025-02-01", "day", "01/02/2025")]
    two = D.assign(found_, (unit(1, 0, 30), unit(2, 31, 80)))
    assert [(d.block_index, d.unit_no, d.status) for d in two] == [(3, 1, "stated"), (40, 2, "stated")]
    one = D.assign(found_, (unit(1, 0, 80),))
    assert [(d.unit_no, d.status) for d in one] == [(1, "conflict"), (1, "conflict")]
    outside = D.assign(found_, (unit(1, 0, 10),))
    assert [d.unit_no for d in outside] == [1, None]
    whole = D.assign(found_, ())  # a version with no units: the document is the scope
    assert [(d.unit_no, d.status) for d in whole] == [(0, "conflict"), (0, "conflict")]


# --- as_of: the three sources, in order ---------------------------------------------------------------------------

def resolve(text, **kw):
    return D.resolve_as_of(text, anchor=kw.pop("anchor", ANCHOR), **kw)


UV = D.UnitValuation("stated", stated(2, "2024-04-15"), (stated(2, "2024-04-15"),), 1)
UV_CONFLICT = D.UnitValuation("conflict", None, (stated(2, "2024-04-15"), stated(9, "2025-01-01")), 1)


def test_a_single_date_in_the_datum_own_sentence_dates_it_with_source_provenance():
    got = resolve("מחיר העסקה ב-12/2023 היה 1,900,000 ₪")
    assert got == {"date": "2023-12", "precision": "month", "end": None, "provenance": "source", "forecast": False,
                   "locator": "text"}


def test_a_table_row_with_a_month_date_cell():
    got = resolve("12/2023 | עסקה | 1,900,000")
    assert (got["date"], got["precision"], got["provenance"]) == ("2023-12", "month", "source")


def test_a_row_with_no_date_is_unknown():
    assert resolve("תל אביב | עסקה | 1,900,000") is None


def test_the_table_caption_range_dates_undated_rows_with_table_provenance():
    got = resolve("תל אביב | עסקה | 1,900,000", table_caption="עסקאות 2021–2022")
    assert (got["date"], got["end"], got["precision"], got["provenance"]) == ("2021", "2022", "year", "source_table")


def test_a_caption_with_no_range_dates_nothing():
    assert resolve("תל אביב | 1,900,000", table_caption="עסקאות בנכסים דומים") is None
    assert resolve("תל אביב | 1,900,000", table_caption="עסקאות בשנת 2022") is None  # KTD4: a range only


def test_the_rows_own_date_wins_over_the_caption_range():
    got = resolve("03/2022 | 1,900,000", table_caption="עסקאות 2021–2022")
    assert (got["date"], got["provenance"]) == ("2022-03", "source")


def test_two_different_dates_in_a_sentence_are_ambiguous_and_never_fall_through():
    assert resolve("בין 03/2022 ל-05/2023 ו-12/2024 נמכרו שלוש דירות", table_caption="עסקאות 2021–2022",
                   stance="adopted", stance_provenance="source", unit_valuation=UV) is None
    assert resolve("ב-15/04/2022 וב-20/05/2023: 1,900,000 ₪", unit_valuation=UV) is None


def test_one_range_in_the_sentence_is_one_date_with_an_end():
    got = resolve("בין 2021 ל-2022 נמכרו הנכסים")
    assert (got["date"], got["end"], got["precision"], got["provenance"]) == ("2021", "2022", "year", "source")


def test_an_adopted_source_backed_conclusion_with_no_date_takes_the_units_valuation_date():
    got = resolve("שווי הנכס 2,450,000 ₪", stance="adopted", stance_provenance="source", unit_valuation=UV)
    assert got == {"date": "2024-04-15", "precision": "day", "end": None, "provenance": "unit_valuation",
                   "forecast": False, "locator": "block 2"}


@pytest.mark.parametrize("stance, provenance", [
    ("claim", "source"), ("estimate", "source"), ("proposal", "source"), ("unknown", "source"),
    ("adopted", "model_asserted"), ("adopted", "not_stated"), ("adopted", None), (None, None),
])
def test_a_value_that_is_not_a_source_backed_adopted_conclusion_stays_unknown(stance, provenance):
    assert resolve("שווי הנכס 2,450,000 ₪", stance=stance, stance_provenance=provenance, unit_valuation=UV) is None


def test_a_conflicting_or_missing_unit_valuation_is_unknown():
    kw = dict(stance="adopted", stance_provenance="source")
    assert resolve("שווי הנכס 2,450,000 ₪", unit_valuation=UV_CONFLICT, **kw) is None
    assert resolve("שווי הנכס 2,450,000 ₪", unit_valuation=D.UnitValuation("none", None, (), 1), **kw) is None
    assert resolve("שווי הנכס 2,450,000 ₪", unit_valuation=None, **kw) is None


def test_a_date_in_the_sentence_is_its_own_date_even_for_an_adopted_conclusion():
    got = resolve("שווי הנכס ביום 01/02/2025 הוא 2,450,000 ₪", stance="adopted", stance_provenance="source",
                  unit_valuation=UV)
    assert (got["date"], got["provenance"]) == ("2025-02-01", "source")


def test_a_price_a_parcel_and_an_area_in_the_sentence_are_no_date():
    assert resolve("חלקה 2024 בגוש 6001, שטח 120 מ״ר, 2,024,000 ₪", stance="adopted", stance_provenance="source",
                   unit_valuation=UV)["provenance"] == "unit_valuation"


# --- forecast -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("sentence", [
    "דמי השכירות צפויים לעמוד ב-2027 על 6,000 ₪",
    "הערך צפוי לעלות", "צפויה עלייה בשכר הדירה", "תחזית לשנת 2027", "המחיר יהיה גבוה יותר", "השכירות תהיה גבוהה",
    "ייתכן שינוי בעתיד", "הערכה ל-2028 על 3,000,000 ₪", "הערכה לשנת 2028", "אפשרויות עתיד", "לעתיד הקרוב",
])
def test_a_forecast_marker_makes_a_forecast(sentence):
    assert D.is_forecast(sentence, None, ANCHOR) is True


@pytest.mark.parametrize("sentence", [
    "דמי השכירות עמדו ב-2022 על 5,000 ₪", "המכר בוצע ביום 15/04/2024", "הערכה לשווי הנכס בוצעה על ידי השמאי",
    "הוצאה בפועל", "",
])
def test_a_past_sentence_without_a_marker_is_not_a_forecast(sentence):
    assert D.is_forecast(sentence, None, ANCHOR) is False


def test_a_date_after_the_anchor_is_a_forecast_by_its_precision():
    def asof(iso, precision, end=None):
        return {"date": iso, "precision": precision, "end": end}

    assert D.is_forecast("מחיר", asof("2026-10-15", "day"), ANCHOR) is True
    assert D.is_forecast("מחיר", asof("2026-10-14", "day"), ANCHOR) is False
    assert D.is_forecast("מחיר", asof("2026-11", "month"), ANCHOR) is True
    assert D.is_forecast("מחיר", asof("2026-10", "month"), ANCHOR) is False  # this month is not later than today
    assert D.is_forecast("מחיר", asof("2027", "year"), ANCHOR) is True
    assert D.is_forecast("מחיר", asof("2026", "year"), ANCHOR) is False
    assert D.is_forecast("מחיר", asof("2025", "year", "2028"), ANCHOR) is True  # a range into the future
    assert D.is_forecast("מחיר", asof("2021", "year", "2022"), ANCHOR) is False


def test_resolve_marks_a_future_date_as_a_forecast_with_no_marker_and_stance_plays_no_part():
    got = resolve("שכר הדירה ב-2027 הוא 6,000 ₪")
    assert (got["date"], got["forecast"]) == ("2027", True)
    got = resolve("שכר הדירה ב-2022 הוא 5,000 ₪", stance="estimate", stance_provenance="source")
    assert (got["date"], got["forecast"]) == ("2022", False)  # `estimate` is a party's estimate, not a forecast


def test_a_forecast_sentence_with_a_past_date_is_a_forecast():
    assert resolve("תחזית: ב-2022 התכוונו לשכר של 5,000 ₪")["forecast"] is True


def test_the_unit_valuation_date_after_the_anchor_is_a_forecast_too():
    future = D.UnitValuation("stated", stated(2, "2027-01-01"), (stated(2, "2027-01-01"),), 1)
    got = resolve("שווי הנכס 2,450,000 ₪", stance="adopted", stance_provenance="source", unit_valuation=future)
    assert got["forecast"] is True


# --- the measurement: the stored date counts only when the quote says it ------------------------------------------

def test_a_stored_date_the_quote_does_not_hold_is_unknown():
    assert D.measurement_as_of("שכר דירה חודשי 5,000 ₪", "2023", anchor=ANCHOR) is None


def test_a_stored_year_the_quote_holds_beside_a_date_word_dates_the_measurement_with_source_provenance():
    got = D.measurement_as_of("בשנת 2023 עמד שכר הדירה על 5,000 ₪", "2023", anchor=ANCHOR)
    assert (got["date"], got["precision"], got["provenance"], got["locator"]) == ("2023", "year", "source", "quote")


def test_a_bare_number_that_equals_the_stored_year_is_no_date():
    assert D.measurement_as_of("שכר דירה 2023 ₪ לחודש", "2023", anchor=ANCHOR) is None


def test_the_stored_date_picks_among_the_quotes_dates_when_it_names_one_of_them():
    quote = "בין 15/04/2022 לבין 20/05/2023 נחתם החוזה ב-20/05/2023, ושכר הדירה 5,000 ₪"
    assert D.resolve_as_of(quote, anchor=ANCHOR) is None  # ambiguous by itself
    got = D.measurement_as_of(quote, "2023-05-20", anchor=ANCHOR)
    assert (got["date"], got["provenance"]) == ("2023-05-20", "source")
    assert D.measurement_as_of(quote, "2021-01-01", anchor=ANCHOR) is None


def test_a_stored_date_of_another_precision_than_the_quote_is_not_the_same_date():
    assert D.measurement_as_of("מיום 15/04/2023 שכר 5,000 ₪ ו-20/05/2023: 5,200 ₪", "2023", anchor=ANCHOR) is None


def test_a_quote_with_its_own_single_date_dates_the_measurement_with_no_stored_date():
    got = D.measurement_as_of("שכר דירה מיום 15/04/2023: 5,000 ₪", None, anchor=ANCHOR)
    assert (got["date"], got["provenance"]) == ("2023-04-15", "source")


def test_a_measurement_forecast_is_marked():
    got = D.measurement_as_of("בשנת 2029 צפוי שכר של 7,000 ₪", "2029", anchor=ANCHOR)
    assert got["forecast"] is True
