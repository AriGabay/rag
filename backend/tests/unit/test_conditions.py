"""The closed-class operator reader of condition completeness (round 18 U5; KTD1; R1-R4; AE1-AE4).

The tokenizer keeps the operators ``normalize_text.STOPWORDS`` drops, so the server can tell "excluding" from "including" and
"only" from nothing. These tests prove what it decides alone (kept, a contradiction hint) and what it leaves to the requirements
call (undecided), on synthetic Hebrew sentences."""

from __future__ import annotations

from app.chat import conditions
from app.chat.conditions import HINT, KEPT, UNDECIDED


def _ops(text: str) -> list[tuple[str, str, bool]]:
    return [(o.cls, o.key, o.negative) for o in conditions.operators(text)]


def test_the_closed_classes_are_read_with_their_polarity():
    assert _ops("הזכויות חלות רק על פי תכנית מאושרת") == [("exclusivity", "only", False)]
    assert _ops("השטח מותר בלבד") == [("exclusivity", "only", False)]
    assert _ops("למעט קומות החניה") == [("exception", "include", True)]
    assert _ops("השטח אינו כולל קומות החניה") == [("exception", "include", True)]  # the negation folds into the word after it
    assert _ops("השטח כולל קומות החניה") == [("exception", "include", False)]
    assert _ops("בכפוף לאישור הוועדה") == [("subjection", "subject", False)]
    assert _ops("הבניין מעל גיל 30 שנה") == [("threshold", "מעל", False)]
    assert _ops("תוספת של לא יותר מ-5%") == [("threshold", "מעל", True)]
    assert _ops("ללא מרפסת") == [("negation", "negation", True)]


def test_an_operator_the_stopword_list_drops_is_still_read():
    from app.extraction.normalize_text import STOPWORDS

    for word in ("לא", "רק", "עד", "אם", "אין"):
        assert word in STOPWORDS
        assert conditions.operators(f"{word} קומה")


def test_a_plain_including_is_no_condition_of_a_source_but_a_negated_one_is():
    assert not conditions.material_clauses("שטח כולל לבנייה | 1,200")
    assert conditions.material_clauses("השטח אינו כולל מרתף") == ["השטח אינו כולל מרתף"]
    assert conditions.material("הערכה בגישת היוון ההכנסות")  # a method
    assert conditions.material("מצב תכנוני מוצע")  # a status
    assert not conditions.material("הטבלה שלהלן מפרטת את זכויות הבנייה בנכס")


def test_same_operator_same_polarity_and_most_of_the_operand_is_kept():
    condition = "הזכויות חלות רק על פי תכנית מאושרת"
    assert conditions.outcome(condition, "השטח הוא 1,200 מ״ר, רק על פי תכנית מאושרת [S1].") == KEPT
    assert conditions.outcome(condition, "השטח הוא 1,200 מ״ר ורק לפי תכנית שאושרה") == KEPT  # a spelling variant of the operand


def test_ae1_a_value_stated_without_its_limit_is_undecided_not_kept():
    condition = "הזכויות חלות רק על פי תכנית מאושרת"
    assert conditions.outcome(condition, "השטח הכולל לבנייה בנכס הוא 1,200 מ״ר [S1].") == UNDECIDED


def test_ae2_another_operator_for_the_same_idea_is_left_to_the_requirements_call():
    assert conditions.outcome("למעט קומות החניה", "השטח הוא 900 מ״ר ללא קומות החניה") == UNDECIDED


def test_ae3_a_lost_exclusion_is_a_contradiction_hint_and_not_a_missing_condition():
    condition = "השטח אינו כולל קומות החניה"
    assert conditions.outcome(condition, "השטח כולל קומות החניה ושווה 900 מ״ר") == HINT
    assert conditions.outcome("למעט קומות החניה", "השטח כולל קומות החניה") == HINT  # "except" and "including" are opposites
    assert conditions.outcome(condition, "השטח אינו כולל את קומות החניה") == KEPT


def test_a_lost_negation_over_the_same_words_is_a_hint():
    assert conditions.outcome("אין היתר בנייה לקומה השלישית", "קיים היתר בנייה לקומה השלישית") == HINT


def test_a_sentence_with_both_polarities_over_the_operand_is_undecided_with_no_hint():
    condition = "השטח אינו כולל קומות החניה"
    assert conditions.outcome(condition, "השטח כולל קומות החניה, ובחישוב אחר אינו כולל קומות החניה") == UNDECIDED


def test_opposite_polarities_on_different_operands_do_not_contradict_the_operand_that_is_kept():
    condition = "השטח אינו כולל מרתף"
    assert conditions.outcome(condition, "השטח כולל חניה ואינו כולל מרתף") == KEPT


def test_a_partial_operand_overlap_is_not_a_contradiction():
    condition = "השטח אינו כולל קומות חניה תת קרקעיות"
    assert conditions.outcome(condition, "השטח כולל קומות מגורים") == UNDECIDED


def test_a_threshold_with_another_number_is_not_kept_and_not_a_contradiction():
    assert conditions.outcome("עד 30 שנה", "השיעור חל עד 30 שנה") == KEPT
    assert conditions.outcome("עד 30 שנה", "השיעור חל עד 40 שנה") == UNDECIDED


def test_a_condition_without_an_operator_is_kept_by_most_of_its_content_words():
    assert conditions.outcome("גישת היוון ההכנסות", "לפי גישת היוון ההכנסות השווי הוא 9,384,615 ₪") == KEPT
    assert conditions.outcome("גישת היוון ההכנסות", "השווי המהוון הוא 9,384,615 ₪") == UNDECIDED


def test_a_condition_with_a_date_needs_that_date_stated():
    assert conditions.outcome("נכון ליום 31.12.2023", "השווי נכון ל-31/12/2023 הוא 9,000") == KEPT
    assert conditions.outcome("נכון ליום 31.12.2023", "השווי נכון ל-30.06.2024 הוא 9,000") == UNDECIDED
