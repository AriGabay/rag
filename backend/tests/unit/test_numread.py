"""The one Hebrew number-word reader (round 22 U10, KTD9, R17). Synthetic phrases only. No database."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.answering import numread
from app.answering.verify import count_words_in, numbers_in


def _values(text: str, **kw) -> list[int]:
    return [w.value for w in numread.number_words(text, **kw)]


@pytest.mark.parametrize(("text", "value"), [
    ("שנים עשר", 12), ("שתים עשרה", 12), ("שבעה עשר", 17), ("אחד עשר", 11), ("אחת עשרה", 11), ("שלוש עשרה", 13),
    ("חמישה עשר", 15), ("שישה עשר", 16), ("תשע עשרה", 19), ("עשר", 10), ("עשרה", 10),
    ("עשרים", 20), ("עשרים וחמישה", 25), ("עשרים ושלוש", 23), ("שלושים", 30), ("חמישים", 50), ("תשעים ותשעה", 99),
    ("מאה", 100), ("מאתיים", 200), ("שלוש מאות", 300), ("מאה עשרים ושלוש", 123), ("ארבע מאות וחמישים", 450),
    ("אלף", 1000), ("אלפיים", 2000), ("שלושת אלפים", 3000), ("עשרים אלף", 20000), ("מאה וחמישים אלף", 150000),
    ("אלף מאתיים", 1200),
])
def test_compounds_tens_hundreds_and_thousands_are_read_as_one_number(text, value):
    assert _values(text) == [value]


@pytest.mark.parametrize(("text", "value"), [
    ("שני", 2), ("שתי", 2), ("שניים", 2), ("שתיים", 2), ("שתים", 2), ("ששה", 6), ("שישה", 6), ("ששת", 6),
    ("חמשה", 5), ("חמישה", 5), ("חמש", 5), ("חמשת", 5), ("שלשה", 3), ("שלושת", 3), ("אחד", 1), ("אחת", 1),
    ("שמונה", 8), ("שמונת", 8), ("תשעה", 9), ("שבעה", 7), ("ארבעת", 4),
])
def test_masculine_feminine_and_construct_forms_agree(text, value):
    assert _values(text) == [value]


@pytest.mark.parametrize(("text", "value"), [
    ("בשלושה", 3), ("ושש", 6), ("לשבעה עשר", 17), ("ב-שנים עשר", 12), ("בשנים עשר תשלומים", 12), ("השתיים", 2),
    ("מעשרים וחמישה", 25), ("ולמאה", 100),
])
def test_prefix_letters_stay_outside_the_value(text, value):
    assert _values(text) == [value]


def test_the_span_of_a_phrase_covers_every_word_and_its_prefix():
    text = "נפרע בשנים עשר תשלומים"
    (w,) = numread.number_words(text)
    assert text[w.start:w.end] == "בשנים עשר"


@pytest.mark.parametrize("text", [
    "השני", "השנייה", "חמישית", "שלישי", "הראשון", "רביעית", "שנים", "המאה", "האלף",
])
def test_ordinals_names_and_the_plural_of_year_are_no_amounts(text):
    assert _values(text) == []


@pytest.mark.parametrize("text", ["תעשרים", "עשרימים", "מעשרות", "עשרוני", "אעשרים", "שישי", "xעשרים", "עשרים1"])
def test_a_number_word_inside_a_longer_word_is_not_read(text):
    assert _values(text) == []


def test_a_scale_word_after_a_digit_is_no_number_of_its_own():
    assert _values("250 אלף ₪") == []
    assert _values("נאמד ב-3 מאות") == []


def test_two_separate_numbers_are_two_phrases():
    assert _values("שלוש חמש") == [3, 5]
    assert _values("שני חדרים ושלוש מרפסות") == [2, 3]


def test_words_read_as_text_values_and_a_phrase_value():
    assert numread.word_forms("שנים עשר ועשרים וחמישה") == {"12", "25"}
    assert numread.word_forms("עשרים") == numbers_in("20") == {"20", "2E+1"}  # a number in words has the forms the same number in digits has
    assert numread.number_word("עשרים וחמישה") == Decimal(25)
    assert numread.number_word("בניין") is None
    assert numread.whole_number_word("שתים עשרה") == 12
    assert numread.whole_number_word("שתים עשרה יחידות") is None


# --- the evidence side: numbers_in(words=True) --------------------------------------------------------------------

@pytest.mark.parametrize(("text", "digits"), [
    ("שנים עשר", "12"), ("שתים עשרה", "12"), ("אחד עשר", "11"), ("שבעה עשר קומות", "17"), ("עשרים וחמישה", "25"), ("חמישים", "50"),
    ("מאה", "100"), ("מאתיים", "200"), ("שישה מועדים", "6"), ("הגרסה השנייה, הבניין החמישי", ""),
])
def test_the_evidence_reader_gives_the_whole_number_as_its_digits_do_not_its_parts(text, digits):
    assert numbers_in(text, words=True) == (numbers_in(digits) if digits else set())


def test_a_claim_without_words_reads_no_words():
    assert numbers_in("שנים עשר תשלומים") == set()


# --- the claim side: a count written in words -------------------------------------------------------------------

def test_a_count_before_a_noun_is_read_on_the_claim_side():
    assert count_words_in("נלקחו ארבע משכנתאות [S1]") == numbers_in("4")
    assert count_words_in("יש שנים עשר תשלומים") == numbers_in("12")
    assert count_words_in("נפרע בעשרים וחמישה תשלומים") == numbers_in("25")


@pytest.mark.parametrize("text", [
    "אחד המסמכים קובע", "בפעם אחת הוסכם", "השני קובע", "השלושה חתמו", "מצד שני",
    "הוסכם על שתיים", "המאה העשרים", "250 אלף ₪",
])
def test_idioms_ones_the_article_and_a_number_with_no_noun_are_not_claim_numbers(text):
    assert count_words_in(text) == set()
