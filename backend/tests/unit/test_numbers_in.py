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
