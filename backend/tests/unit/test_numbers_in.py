"""``numbers_in`` reads a date as its day, month and year whatever sentence punctuation follows it, and keeps a real
decimal at a sentence end a decimal (round 9 U12, KTD10, R22, AE7). Synthetic text only."""

from __future__ import annotations

import pytest

from app.answering.verify import numbers_in

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
