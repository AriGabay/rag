"""``numbers_in`` reads a date as its day, month and year whatever sentence punctuation follows it, and keeps a real
decimal at a sentence end a decimal (round 9 U12, KTD10, R22, AE7). Synthetic text only."""

from __future__ import annotations

import pytest

from app.answering.numread import decimal_convention, read_separators
from app.answering.verify import check_claim, numbers_in

DATE_PARTS = {"1", "11", "1966"}


@pytest.mark.parametrize("tail", ["", ".", ",", ";", ":", ")", ".\n", ", ממנו"])
def test_a_date_is_day_month_and_year_whatever_follows_it(tail):
    got = numbers_in(f"היתר הבנייה הוא מס׳ 638, מיום 01.11.1966{tail}")
    assert got == DATE_PARTS | {"638"}


def test_a_date_with_and_without_the_closing_period_gives_the_same_numbers():
    assert numbers_in("מיום 01.11.1966.") == numbers_in("מיום 01.11.1966,") == numbers_in("מיום 01.11.1966")


def test_a_date_at_a_sentence_end_is_not_read_as_a_decimal():
    got = numbers_in("בתוקף עד 28.12.2022.")
    assert got == {"28", "12", "2022"} and "28.12" not in got


def test_a_date_in_parentheses_and_with_slashes_or_two_digit_year():
    assert numbers_in("(מיום 14/02/2023)") == {"14", "2", "2023"}
    assert numbers_in("מיום 14.2.23.") == {"14", "2", "23"}


def test_a_real_decimal_at_a_sentence_end_stays_a_decimal():
    assert numbers_in("המקדם הוא 2.5.") == {"2.5"}
    assert numbers_in("המקדם הוא 2.5, והשאר 3.") == {"2.5", "3"}


def test_a_dotted_chain_that_continues_is_not_a_date():
    # a section number or a version ("1.2.3.4") keeps its earlier reading: the date needs its end
    assert "1.2" in numbers_in("סעיף 1.2.3.4 בתקנון")


# --- a comma is a thousands separator only in groups of three (round 18 U2, KTD3, R8) ---------------------------------

def test_a_comma_joined_list_of_years_reads_as_each_year():
    years = numbers_in("2018") | numbers_in("2019") | numbers_in("2021")
    assert numbers_in("ממוצע השנים 2018,2019,2021") == years
    assert numbers_in("השנים 2018,2019") == numbers_in("2018") | numbers_in("2019")
    assert numbers_in("השנים 2018, 2019 ו-2021") == years


@pytest.mark.parametrize("text, plain", [
    ("1,250,000", "1250000"), ("12,500", "12500"), ("1,250.5", "1250.5"), ("999", "999"),
    ("סך הכל 1,250,000.", "סך הכל 1250000."),
])
def test_numbers_with_thousands_separators_read_as_before(text, plain):
    assert numbers_in(text) == numbers_in(plain)
    assert "1250000" in numbers_in("1,250,000") and "12500" in numbers_in("12,500")


def test_two_numbers_the_list_joins_are_not_one_thousands_number():
    assert numbers_in("סך הכל 1,250,000, והשאר 12,500.") == numbers_in("1250000") | numbers_in("12500")
    assert numbers_in("2018,201") == numbers_in("2018") | numbers_in("201")
    assert numbers_in("12,5678") == numbers_in("12") | numbers_in("5678")


# --- a comma is read by context and by the document's convention; a sign is part of the number (round 19 U1, KTD3, R11, R12, AE6) ---------


def nums(*args, **kwargs):
    """``numbers_in`` without the scientific spellings of round numbers ("1E+3" next to "1000")."""
    return {f for f in numbers_in(*args, **kwargs) if "E" not in f}


@pytest.mark.parametrize("text", ["1.234,56", "1,234.56", "סכום 1.234,56 ₪", "סכום 1,234.56 ₪"])
def test_when_both_separators_appear_the_later_one_is_the_decimal(text):
    assert nums(text) == {"1234.56"}


def test_a_european_number_with_several_thousands_points_reads_as_one_number():
    assert nums("שווי 1.234.567,89 ₪") == {"1234567.89"}
    assert len("1.234,56") == len(read_separators("1.234,56"))  # positions stay valid


def test_a_comma_after_a_zero_is_a_decimal_comma_whatever_follows():
    assert nums("מקדם 0,125") == {"0.125"}
    assert nums("0,5") == {"0.5"}


@pytest.mark.parametrize("text", [
    "13,2%", "שיעור של 13,2 %", "13,2 מ\"ר", "₪ 13,2", "€ 13,2", "| שטח | 13,2 |", "13,2",
])
def test_a_single_comma_with_one_or_two_digits_is_decimal_next_to_a_unit_or_as_a_whole_cell(text):
    assert nums(text) == {"13.2"}


def test_a_single_comma_with_one_or_two_digits_in_a_sentence_stays_a_list_for_a_claim():
    assert nums("בקומות 3,4 יש חדרים") == {"3", "4"}
    assert nums("חדרים 3,4,5") == {"3", "4", "5"}


def test_a_source_keeps_both_readings_of_an_ambiguous_pair_and_never_reads_a_chain_as_a_decimal():
    assert nums("בקומות 3,4 יש חדרים", source=True) == {"3.4", "3", "4"}
    assert nums("בקומות 3,4 יש חדרים", words=True) == {"3.4", "3", "4"}
    assert nums("חדרים 6,7,8", source=True) == {"6", "7", "8"}  # AE6: a list 6,7,8 is no 6.7
    assert "6.7" not in nums("חדרים 6,7,8")


def test_a_decimal_convention_of_the_text_decides_its_other_pairs():
    text = "המקדם 0,125 ודמי הניהול בקומות 3,4"
    assert nums(text) == {"0.125", "3.4"}
    assert decimal_convention(text) and not decimal_convention("בקומות 3,4")
    assert nums("בקומות 3,4", decimal=True) == {"3.4"}


def test_thousands_stay_thousands():
    assert nums("3,596,696") == {"3596696"}
    assert nums("1,250") == {"1250"}
    assert nums("שטח 13,2 מ\"ר ושווי 3,596,696 ₪") == {"13.2", "3596696"}
    assert "132" not in nums("13,2 מ\"ר", source=True) and "13" not in nums("13,2 מ\"ר", source=True)


def test_ae6_a_claim_of_13_2_is_supported_by_a_source_cell_13_2_and_13_or_132_are_not():
    source = "שטח | שווי\n13,2 | 3,596,696"
    ev = {"E1": source}
    ok = lambda claim: check_claim(claim, ["E1"], [], "explicit", evidence=ev, computed_numbers=set()) == []  # noqa: E731
    assert ok("השטח הוא 13.2")
    assert ok("השטח הוא 13,2")  # the claim writes the source's own convention
    assert ok("השווי הוא 3,596,696")
    assert not ok("השטח הוא 13")
    assert not ok("השטח הוא 132")


def test_a_claim_reads_a_pair_as_decimal_only_under_a_decimal_source():
    # source with no decimal convention: the pair is both readings, the claim "3,4" is the list 3 and 4
    ev = {"E1": "בקומות 3,4 יש חדרים"}
    assert check_claim("בקומות 3,4", ["E1"], [], "explicit", evidence=ev, computed_numbers=set()) == []
    # source with a decimal convention: "3,4" is the decimal, the list reading is not the source's
    ev = {"E1": "המקדם 0,125 ובקומות 3,4"}
    assert check_claim("המקדם 3,4", ["E1"], [], "explicit", evidence=ev, computed_numbers=set()) == []
    assert "unsupported_number" in check_claim("בקומות 3 ו-4", ["E1"], [], "explicit", evidence=ev, computed_numbers=set())


# signs ----------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["-3.5%", "−3.5%", "ירידה של -3.5%", "(3.5)%"])
def test_a_negative_source_number_has_the_signed_and_the_unsigned_forms(text):
    assert nums(text, source=True) == {"3.5", "-3.5"}


def test_accounting_brackets_and_a_trailing_minus_are_negative_in_a_source_only():
    assert nums("(₪ 1,197,500)", source=True) == {"1197500", "-1197500"}
    assert nums("סה\"כ הוצאות 104,610-", source=True) == {"104610", "-104610"}
    assert nums("(3.5)%") == {"3.5"} and nums("השווי (5,000 ₪)") == {"5000"}  # a claim's brackets are prose


@pytest.mark.parametrize("text, expected", [
    ("ב-2019", {"2019"}), ("בשנים 10-20", {"10", "20"}), ("דירות 2-3 חדרים", {"2", "3"}), ("5 -10", {"5", "10"}),
    ("- 3 חדרים", {"3"}), ("מ-5,000 ₪", {"5000"}), ("קומה 3-", {"3"}), ("03-1234567", {"3", "1234567"}),
    ("14-02-2023", {"14", "2", "2023"}),
])
def test_a_hyphen_between_words_digits_or_after_a_prefix_is_no_minus(text, expected):
    assert nums(text) == expected


def test_a_negative_claim_needs_a_negative_source_and_an_unsigned_claim_passes():
    def ok(claim, source):
        return check_claim(claim, ["E1"], [], "explicit", evidence={"E1": source}, computed_numbers=set()) == []

    assert not ok("השינוי הוא -3.5%", "השינוי הוא 3.5%")
    assert not ok("השינוי הוא −3.5%", "בטבלה: +3.5%")
    assert ok("השינוי הוא -3.5%", "השינוי הוא -3.5%")
    assert ok("השינוי הוא -3.5%", "השינוי (3.5)%")
    assert ok("השינוי הוא -3.5%", "הירידה היא 3.5-")
    assert ok("השינוי הוא 3.5%", "השינוי הוא -3.5%")
