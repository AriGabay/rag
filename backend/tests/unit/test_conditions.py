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


def test_ae1_a_value_stated_without_its_limit_is_missing_when_the_class_and_operand_are_absent_and_undecided_when_the_operand_is_stated():
    condition = "הזכויות חלות רק על פי תכנית מאושרת"
    assert conditions.outcome(condition, "השטח הכולל לבנייה בנכס הוא 1,200 מ״ר [S1].") == conditions.MISSING
    assert conditions.outcome(condition, "השטח הכולל לבנייה בנכס הוא 1,200 מ״ר לפי תכנית [S1].") == UNDECIDED


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


# --- a condition the sentence plainly leaves out, decided without the requirements call (round 18 U5 follow-up; R1) ------------------

PLAN_NOTE = "הזכויות חלות רק על פי תכנית מאושרת"


def test_a_condition_whose_operator_class_and_operand_are_both_absent_from_the_sentence_is_missing_without_a_judge():
    sentence = "השטח הכולל לבנייה בנכס הוא 1,200 מ״ר [S1]."
    assert conditions.outcome(PLAN_NOTE, sentence) == conditions.MISSING
    assert conditions.missing_quote(PLAN_NOTE, sentence) == PLAN_NOTE
    # only the clause that holds the missing operator is the quote
    assert conditions.missing_quote("השטח אינו כולל מרתף; הערה כללית על המדידה", "הוא 900 מ״ר") == "השטח אינו כולל מרתף"


def test_another_word_of_the_same_class_is_never_missing():
    # "בלבד" and "רק" are both exclusivity: the class is there, so the sentence is read as before (kept, or the judge decides)
    assert conditions.outcome(PLAN_NOTE, "החלות היא על פי תכנית שאושרה בלבד [S1].") != conditions.MISSING
    assert conditions.outcome(PLAN_NOTE, "הזכויות חלות בלבד לפי תוכנית שפורסמה [S1].") != conditions.MISSING
    assert conditions.outcome("השטח אינו כולל מרתף", "השטח הוא 900 מ״ר ללא מרתף") != conditions.MISSING  # negation folds into the exception class
    assert conditions.outcome("השטח אינו כולל מרתף", "השטח כולל חניה") != conditions.MISSING  # the class is there, on another operand


def test_the_operand_stated_without_the_class_is_undecided_and_goes_to_the_judge():
    assert conditions.outcome(PLAN_NOTE, "השטח הכולל לבנייה בנכס הוא 1,200 מ״ר לפי תכנית מאושרת [S1].") == UNDECIDED
    assert conditions.outcome("עד 30 שנה", "הגיל הוא 30 שנה") == UNDECIDED  # the number and the word are there; the threshold is not


def test_an_open_class_condition_with_no_operator_is_never_missing():
    assert conditions.outcome("גישת היוון ההכנסות", "השווי הוא 9,384,615 ₪") == UNDECIDED
    assert conditions.outcome("מצב תכנוני מוצע", "השטח הוא 900 מ״ר") == UNDECIDED


def test_a_condition_with_one_missing_and_one_undecided_operator_goes_to_the_judge():
    condition = "הזכויות חלות רק על פי תכנית מאושרת, בכפוף לאישור הוועדה"
    assert conditions.outcome(condition, "הזכויות הן 1,200 מ״ר לפי אישור הוועדה") == UNDECIDED


def test_a_condition_with_an_operator_and_no_operand_is_never_missing():
    assert conditions.outcome("בלבד", "השטח הוא 900 מ״ר") != conditions.MISSING


# --- the condition is written only from the item's own scope (round 20 U3, KTD3, R10) ----------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from app.chat import verify  # noqa: E402


def _state(**kw) -> dict:
    return {"id": "I1", "source": "S1", "value": "6 מעל הכניסה ו-2 קומות חניה", "quote": "מספר קומות | 6 מעל הכניסה ו-2 קומות חניה",
            "condition_spans": [{"place": "note", "text": "רק על פי תכנית מאושרת"}], "condition_unit": 0, "condition_foreign": [], **kw}


def test_a_missing_condition_that_is_the_items_whole_value_or_whole_quote_is_no_condition():
    st = _state()
    assert verify._whole_value("6 מעל הכניסה ו-2 קומות חניה", st) and verify._whole_value("מספר קומות | 6 מעל הכניסה ו-2 קומות חניה", st)
    assert verify._whole_value("  6  מעל הכניסה   ו-2 קומות חניה ", st)  # blanks are no difference
    assert not verify._whole_value("רק על פי תכנית מאושרת", st) and not verify._whole_value("", st)


def test_the_clause_another_items_span_holds_and_the_sentence_states_is_the_quote_of_the_other_item_hint():
    other = _state(id="I2", condition_spans=[{"place": "note", "text": "חל עד שנה מהמסירה"}])
    unit = verify.Unit(0, "השכירות לדירה גדולה היא 5,000 עד שנה מהמסירה [S2].", "השכירות לדירה גדולה היא 5,000 עד שנה מהמסירה", ["S2"])
    mine = _state(quote="דירה גדולה | 5,000", condition_foreign=["I2"])
    assert verify._foreign_quote(mine, [mine, other], unit) == "חל עד שנה מהמסירה"
    assert verify._foreign_quote(_state(quote="דירה גדולה | 5,000"), [mine, other], unit) == ""  # the ledger named no other item
    quiet = verify.Unit(0, "השכירות לדירה גדולה היא 5,000 [S2].", "השכירות לדירה גדולה היא 5,000", ["S2"])
    assert verify._foreign_quote(mine, [mine, other], quiet) == ""  # a sentence that does not state the clause holds nothing of it


def test_another_sentence_states_the_condition_only_when_it_cites_the_items_source_and_keeps_every_span():
    st = _state()
    ws = SimpleNamespace(values={})
    shared = verify.Unit(0, "השטח 6 מעל הכניסה בשומה הראשונה [S1][S2].", "השטח 6 מעל הכניסה בשומה הראשונה", ["S1", "S2"])
    states = verify.Unit(1, "הזכויות רק על פי תכנית מאושרת [S1].", "הזכויות רק על פי תכנית מאושרת", ["S1"])
    elsewhere = verify.Unit(2, "הזכויות רק על פי תכנית מאושרת [S2].", "הזכויות רק על פי תכנית מאושרת", ["S2"])
    silent = verify.Unit(3, "הזכויות בנכס [S1].", "הזכויות בנכס", ["S1"])
    assert verify._sibling_states(st, shared, [shared, states], set(), ws)
    assert not verify._sibling_states(st, shared, [shared, elsewhere], set(), ws)  # it cites another appraisal's source
    assert not verify._sibling_states(st, shared, [shared, silent], set(), ws)  # it cites the source and states nothing
    assert not verify._sibling_states(st, shared, [shared, states], {1}, ws)  # the sentence was removed
    assert not verify._sibling_states(_state(condition_spans=[]), shared, [shared, states], set(), ws)  # an item with no condition


def test_a_sentence_speaks_of_several_appraisals_when_what_it_cites_is_in_more_than_one_context():
    one = SimpleNamespace(version_id="v", contexts=[1], unit=None)
    two = SimpleNamespace(version_id="v", contexts=[2], unit=None)
    ws = SimpleNamespace(sources={"S1": one, "S2": two}, values={}, measurements={})
    assert verify._speaks_of_several(verify.Unit(0, "", "", ["S1", "S2"]), ws)
    assert not verify._speaks_of_several(verify.Unit(0, "", "", ["S1"]), ws)
    assert not verify._speaks_of_several(verify.Unit(0, "", "", []), ws)
