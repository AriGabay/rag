"""The shared date parser (round 11 U6a: R5, R6; KTD4 "Partial dates" and "Parsing").

The format table below is the contract: every format the parser accepts with the precision it gives, every text it
refuses (unknown, never a guess), the numbers `find_all` must not read as years, and the spans. All texts are
synthetic."""

from __future__ import annotations

import pytest

from app.platform import dates as D
from app.professional import profile as P

# --- accepted formats: (text, iso, precision, end) ----------------------------------------------------------------

DAY = [
    ("15/04/2024", "2024-04-15"), ("05/10/2025", "2025-10-05"), ("5/10/2025", "2025-10-05"),
    ("15.4.2024", "2024-04-15"), ("15.04.2024", "2024-04-15"), ("15-04-2024", "2024-04-15"),
    ("2024.04.15", "2024-04-15"), ("2024-04-15", "2024-04-15"), ("2024/04/15", "2024-04-15"),
    ("29/02/2024", "2024-02-29"),
    ("15 באפריל 2024", "2024-04-15"), ("15 אפריל 2024", "2024-04-15"), ("1 בינואר 2026", "2026-01-01"),
    ("15 לאפריל 2024", "2024-04-15"),
]
MONTH = [
    ("03/2024", "2024-03"), ("3/2024", "2024-03"), ("12/2023", "2023-12"), ("3.2024", "2024-03"),
    ("אפריל 2024", "2024-04"), ("באפריל 2024", "2024-04"), ("לאפריל 2024", "2024-04"), ("מאפריל 2024", "2024-04"),
    ("מרץ 2026", "2026-03"), ("מרס 2026", "2026-03"), ("במאי 2022", "2022-05"), ("ובאוקטובר 2025", "2025-10"),
]
YEAR = [
    ("בשנת 2024", "2024"), ("שנת 2024", "2024"), ("לשנת 2024", "2024"), ("ב-2024", "2024"), ("מ-2020", "2020"),
    ("עד 2024", "2024"), ("בין 2024", "2024"), ("ל-2024", "2024"), ("נכון ל-2024", "2024"), ("ב־2024", "2024"),
]


@pytest.mark.parametrize("text,iso", DAY)
def test_a_day_date_parses_with_day_precision(text, iso):
    p = D.parse(text)
    assert p is not None and (p.iso, p.precision, p.end) == (iso, "day", None)


@pytest.mark.parametrize("text,iso", MONTH)
def test_a_month_date_is_a_month_and_never_the_first_of_it(text, iso):
    p = D.parse(text)
    assert p is not None and (p.iso, p.precision, p.end) == (iso, "month", None)
    assert len(p.iso) == 7


@pytest.mark.parametrize("text,iso", YEAR)
def test_a_year_after_a_date_word_is_a_year(text, iso):
    p = D.parse(text)
    assert p is not None and (p.iso, p.precision, p.end) == (iso, "year", None)


@pytest.mark.parametrize("text,iso,end,precision", [
    ("2021–2022", "2021", "2022", "year"), ("2021-2022", "2021", "2022", "year"), ("2021 – 2022", "2021", "2022", "year"),
    ("2021־2022", "2021", "2022", "year"),
    ("בין 2020 ל-2023", "2020", "2023", "year"), ("בין 2020 לבין 2023", "2020", "2023", "year"),
    ("בין 2020 ו-2023", "2020", "2023", "year"), ("מ-2020 עד 2023", "2020", "2023", "year"),
    ("מ-2020 ועד 2023", "2020", "2023", "year"), ("2020 עד 2023", "2020", "2023", "year"),
    ("מ-01/2021 עד 03/2022", "2021-01", "2022-03", "month"),
    ("בין מרץ 2020 לאפריל 2022", "2020-03", "2022-04", "month"),
    ("מאפריל 2021 עד מרץ 2022", "2021-04", "2022-03", "month"),
    ("בין 01/04/2021 ל-30/06/2021", "2021-04-01", "2021-06-30", "day"),
    ("01/04/2021-30/06/2021", "2021-04-01", "2021-06-30", "day"),
    # different precisions: both ends are cut to the coarser one, so the range only covers, never invents a month
    ("בין מרץ 2020 ל-2023", "2020", "2023", "year"),
])
def test_a_range_has_a_start_and_an_end_of_one_precision(text, iso, end, precision):
    p = D.parse(text)
    assert p is not None and (p.iso, p.end, p.precision) == (iso, end, precision)


@pytest.mark.parametrize("text", ["2024-2021", "בין 2023 ל-2020", "מ-2023 עד 2021"])
def test_a_backwards_range_is_no_range(text):
    assert all(p.end is None for p in D.find_all(text))
    assert D.parse(text) is None or D.parse(text).end is None


# --- refusals: unknown, never a guess --------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "", "   ", "מחר", "אפריל", "15 באפריל",                       # no date / no year
    "15/04/24", "15.4.24", "04/24", "אפריל 24",                    # two-digit years
    "י״ב באדר תשפ״ד", "תשפ״ד", "ט״ו בניסן תשפ״ד", "שנת תשפ״ד",     # Hebrew-calendar years
    "Q1 2024", "ברבעון הראשון של 2024", "רבעון 3 2024", "2024 Q2",  # quarters
    "31/02/2024", "29/02/2023", "31/04/2024", "00/01/2024", "15/13/2024", "04/15/2024", "2024-13-01", "2024-02-30",
    "31 באפריל 2024", "32 במאי 2024", "13/2024", "00/2024",         # impossible dates
    "2024",                                                        # a bare year with no date word
    "1949", "בשנת 1949", "בשנת 2101",                              # outside 1950-2100
    "חלקה 2024", "גוש 6158", "12345", "1,950,000", "2024.5.7.1",    # numbers that are not dates
])
def test_what_is_no_supported_date_is_unknown(text):
    assert D.parse(text) is None


def test_an_impossible_day_is_not_read_as_the_month_without_it():
    # "31 באפריל 2024" must not fall back to "אפריל 2024" (that would be a guess at the precision)
    assert D.find_all("ב-31 באפריל 2024") == []
    assert D.find_all("הועבר ב-31/02/2024 לבעלים") == []


def test_two_dates_in_one_text_are_not_one_expression():
    assert D.parse("15/04/2024 (עודכן 20/04/2024)") is None


# --- find_all: dates inside a sentence ---------------------------------------------------------------------------------

def test_find_all_finds_every_expression_with_its_span_in_text_order():
    s = "המועד הקובע 15/04/2024 והדו״ח נערך באפריל 2025, בשנת 2023 נמכרה דירה."
    found = D.find_all(s)
    assert [(p.iso, p.precision) for p in found] == [("2024-04-15", "day"), ("2025-04", "month"), ("2023", "year")]
    for p in found:
        assert s[p.span[0]:p.span[1]] == p.written
    assert found[0].written == "15/04/2024"
    assert found[1].written == "אפריל 2025"          # the prefix letter is not part of the date
    assert found[2].written == "2023"                # nor is the date word


def test_the_span_of_a_range_covers_both_ends():
    s = "העסקאות בין 2020 ל-2023 באזור"
    (p,) = D.find_all(s)
    assert (p.iso, p.end) == ("2020", "2023")
    assert s[p.span[0]:p.span[1]] == p.written
    assert p.written.startswith("2020") and p.written.endswith("2023")


def test_a_range_is_one_result_not_three():
    assert len(D.find_all("בין 2020 ל-2023")) == 1
    assert len(D.find_all("2021–2022")) == 1


def test_a_full_date_is_not_also_read_as_its_month_or_year():
    assert len(D.find_all("ב-15/04/2024")) == 1
    assert len(D.find_all("ב-15 באפריל 2024")) == 1
    assert len(D.find_all("בשנת 2024-04-15")) == 1


def test_find_all_returns_nothing_for_plain_text():
    assert D.find_all("") == []
    assert D.find_all("אין כאן תאריך") == []


# --- masks: numbers that are not years -----------------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "גוש 2024 חלקה 1999", "בגוש 6158 חלקה 2024", "חלקה 12/2024", "חלקות 2024", "מגרש 1999", "במגרש מס' 2005",
    "תקן 2024", "תקן 1.2024", "לפי תקן ישראלי 2000", "סעיף 2024", "סעיף 3.2024", "בסעיף 2020",
    "ברחוב הרצל 1999", "רחוב הרצל 12/2024", "ברחוב שדרות בן גוריון 2005", "רח' הדוגמה 2010",
    "המחיר 2024 ₪", "בשנת 2000 ₪", "עד 2500 ₪", "₪ 2024", "₪2,024", "עד 2000 ש״ח", 'עד 2000 ש"ח', "ב-1990 שקלים",
    "1,950,000", "2,024", "1.950.000", "בסך 1,2024",
    "שטח של עד 1999 מ״ר", 'שטח 2000 מ"ר', "בין 1960 ל-2100 מ״ר", "ל-2024 דונם", "מ-2000 מטר", "עד 2000 מ'",
    "ב-2024%", "עד 2000 אחוז", "ב-1999 אחוזים",
    "ברבעון 1 בשנת 2024", "Q1 ב-2024", "2024 Q1",
])
def test_numbers_that_are_not_dates_are_not_found(text):
    assert D.find_all(text) == []


def test_a_mask_only_covers_its_own_number():
    s = "בגוש 6158 חלקה 12 נמכרה דירה בשנת 2022, בסך 1,950,000 ₪"
    assert [p.iso for p in D.find_all(s)] == ["2022"]
    s = "תקן 2000 פורסם ב-15/04/2024"
    assert [p.iso for p in D.find_all(s)] == ["2024-04-15"]


def test_a_street_before_a_date_word_does_not_mask_the_year():
    assert [p.iso for p in D.find_all("ברחוב הרצל נמכרה דירה ב-2020")] == ["2020"]
    assert [p.iso for p in D.find_all("רחוב הרצל 1, שנת 2021")] == ["2021"]


# --- the behavior of profile.parse_date for its existing callers is unchanged and agrees ----------------------------------

@pytest.mark.parametrize("raw", ["05.10.2025", "5/10/2025", "15 בינואר 2026", "אוקטובר 2025", "באוקטובר 2025", "מרס 2026"])
def test_the_shared_parser_agrees_with_the_profile_parser(raw):
    assert D.parse(raw).iso == P.parse_date(raw)


@pytest.mark.parametrize("raw", ["31.02.2025", "אוקטובר", "13/13/2025", "", "מחר"])
def test_the_profile_parser_still_rejects_what_it_rejected(raw):
    assert P.parse_date(raw) is None
    assert D.parse(raw) is None


def test_the_profile_parser_keeps_ignoring_the_new_formats():
    # profile.parse_date is not extended: the new formats live in the shared parser only
    assert P.parse_date("2024") is None
    assert P.parse_date("03/2024") is None
    assert P.parse_date("2024.04.15") is None
