"""The qualifier edit (round 12 U10, KTD10, R7, AE5): a number the sources back survives when only a qualifier the answer
gives it — a period, an area basis, a VAT status — is one its source does not state. The edit runs after the repair rounds
and before claim removal; it removes the qualifier's own words (or, when the question asked about that attribute, writes
that the source does not state it), is recorded in the removal note as an edit and not a removal, and the edited sentence
is judged again in the final check. A source that states a different value, or a number that fails its own check, keeps
the removal path. The judge is scripted; synthetic text only."""

from __future__ import annotations

import re

import pytest

from app.chat import coverage, final_check
from app.chat.verify import TurnRequirements, verify_answer
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider
from tests.unit.test_verify_units import _answer, _judge, _registered

RENT = "שכר דירה 5,000 ₪"  # the quote states no period
RENT_MONTHLY = "שכר דירה חודשי 5,000 ₪"
RENT_YEARLY = "שכר דירה 5,000 ₪ לשנה"
ASSERTED = {"period": "model_asserted"}
EDIT_NOTE = "הוסר ניסוח"  # the removal note's line for an edit starts with this
REMOVAL_NOTE = "הוסרו מהתשובה"


def _report(markdown: str, ws, question: str = "?"):
    a = _answer(markdown)
    return a, verify_answer(_judge(lambda t: "supported"), a, ws, question, [])


def _silent_period(**kw):
    return _registered("5,000", RENT, period="month", provenance=ASSERTED, **kw)


# --- the edit: only the unsupported words go, the number stays (AE5) -----------------------------------------------------

def test_a_period_the_source_does_not_state_is_removed_and_the_number_stays():
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש [V1].", _silent_period())
    # the blocking meaning problem is still the trigger (U9), but it does not remove the claim
    assert [p.check for p in r.problems] == ["meaning"] and r.problems[0].failure_kind == "wrong_unit"
    assert r.removed_units() == set() and r.removals() == [] and r.failed_units() == set()
    out = r.edit(a)
    assert out.doc.text.startswith("שכר הדירה הוא 5,000 ₪ [V1].")
    assert "לחודש" not in out.doc.text.split("\n")[0]


def test_the_removal_note_records_the_edit_and_counts_it_as_an_edit_not_a_removal():
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש [V1].", _silent_period())
    text = r.apply(a).answer_markdown
    note = text.split("\n\n", 1)[1]
    assert note.startswith(">") and EDIT_NOTE in note and REMOVAL_NOTE not in note
    counts = r.counts()
    assert counts["removed"] == 0 and counts["edited"] == 1 and counts["removals"] == []
    assert counts["correctness"] == "partial"
    assert [row.action for row in r.logged()] == ["qualified"]


def test_the_edit_waits_for_the_repair_rounds():
    """The problem is still repairable: the engine gives the model its rounds first (the edit is the last step)."""
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש [V1].", _silent_period())
    assert not r.ok and r.repair_keys() and "5,000" in r.problems_text()


def test_the_edit_is_a_deletion_the_server_registers_no_span_for_but_the_note():
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש [V1].", _silent_period())
    edited = r.edit(a)
    assert [s.kind for s in edited.doc.spans()] == ["removal_note"]


def test_a_question_that_named_the_period_gets_it_written_as_not_stated():
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש [V1].", _silent_period(), "מה שכר הדירה לחודש?")
    edited = r.edit(a)
    first = edited.doc.text.split("\n")[0]
    assert first == "שכר הדירה הוא 5,000 ₪ (התקופה לא צוינה במקור) [V1]."
    assert [s.kind for s in edited.doc.spans()][0] == "qualifier"
    assert r.removed_units() == set()


def test_a_basis_the_source_does_not_state_is_removed_and_a_stated_period_stays():
    ws = _registered("5,000", RENT_MONTHLY, period="month")  # the quote states the period only
    a, r = _report("שכר הדירה הוא 5,000 ₪ ברוטו לחודש [V1].", ws)
    assert [p.check for p in r.problems] == ["meaning"]
    assert r.edit(a).doc.text.startswith("שכר הדירה הוא 5,000 ₪ לחודש [V1].")


def test_a_per_area_amount_keeps_its_unit_when_only_the_basis_goes():
    quote = "דמי שכירות 96 ₪ למ\"ר"
    ws = _registered("96", quote, unit="ILS_per_sqm", area_basis="פלדלת", provenance={"area_basis": "model_asserted"})
    a, r = _report("דמי השכירות הם 96 ₪ למ\"ר פלדלת [V1].", ws)
    assert r.removed_units() == set()
    assert r.edit(a).doc.text.startswith("דמי השכירות הם 96 ₪ למ\"ר [V1].")


def test_a_vat_status_the_source_does_not_state_is_removed_with_its_comma():
    ws = _registered("2,450,000", "שווי הנכס: 2,450,000 ₪", vat="included", provenance={"vat": "model_asserted"})
    a, r = _report("שווי הנכס הוא 2,450,000 ₪, כולל מע\"מ [V1].", ws)
    assert [p.check for p in r.problems] == ["vat"] and r.removed_units() == set()
    assert r.edit(a).doc.text.startswith("שווי הנכס הוא 2,450,000 ₪ [V1].")


def test_a_question_that_named_the_vat_gets_it_written_as_not_stated():
    ws = _registered("2,450,000", "שווי הנכס: 2,450,000 ₪", vat="included", provenance={"vat": "model_asserted"})
    a, r = _report("שווי הנכס הוא 2,450,000 ₪, כולל מע\"מ [V1].", ws, "מה שווי הנכס, כולל מע\"מ?")
    assert r.edit(a).doc.text.startswith("שווי הנכס הוא 2,450,000 ₪ (המע\"מ לא צוין במקור) [V1].")


def test_two_unsupported_qualifiers_of_one_number_are_both_edited():
    ws = _registered("5,000", RENT, period="month", provenance=ASSERTED)
    a, r = _report("שכר הדירה הוא 5,000 ₪ ברוטו לחודש [V1].", ws)
    assert r.removed_units() == set()
    assert r.edit(a).doc.text.startswith("שכר הדירה הוא 5,000 ₪ [V1].")


# --- what the edit never touches ------------------------------------------------------------------------------------------

def test_a_source_that_states_another_period_keeps_the_removal_path():
    ws = _registered("5,000", RENT_YEARLY, period="year")
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש [V1].", ws)
    assert r.removed_units() == {0} and r.counts()["edited"] == 0
    assert r.removals()[0].check == "meaning" and "5,000" not in r.apply(a).answer_markdown.split("\n\n")[0]


def test_a_source_that_states_another_vat_keeps_the_removal_path():
    ws = _registered("2,450,000", "שווי הנכס: 2,450,000 ₪ כולל מע\"מ", vat="included")
    a, r = _report("שווי הנכס הוא 2,450,000 ₪, לא כולל מע\"מ [V1].", ws)
    assert r.removed_units() == {0} and r.removals()[0].check == "vat"


def test_a_unit_with_one_edit_and_one_correction_is_removed_whole():
    """The period is silent, but the VAT is stated differently: the unit is wrong, not merely over-qualified."""
    ws = _registered("5,000", "שכר דירה 5,000 ₪ כולל מע\"מ", period="month", vat="included", provenance=ASSERTED)
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש, לא כולל מע\"מ [V1].", ws)
    assert r.removed_units() == {0} and r.counts()["edited"] == 0


def test_a_unit_whose_number_fails_its_check_is_not_edited_and_is_removed_as_before():
    a, r = _report("שכר הדירה הוא 7,000 ₪ לחודש [V1].", _silent_period())
    assert r.removed_units() == {0} and r.removals()[0].check == "unstated_number"
    assert r.counts()["edited"] == 0 and "7,000" not in r.apply(a).answer_markdown.split("\n\n")[0]


def test_a_unit_with_a_second_number_that_fails_is_removed_whole():
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש, והארנונה 800 ₪ [V1].", _silent_period())
    assert r.removed_units() == {0} and r.counts()["edited"] == 0


def test_a_supported_period_is_not_touched():
    ws = _registered("5,000", RENT_MONTHLY, period="month")
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש [V1].", ws)
    assert r.problems == [] and r.edit(a).doc.text == a.answer_markdown and r.counts()["edited"] == 0


def test_an_edit_in_one_unit_leaves_the_other_units_and_the_numbers_alone():
    md = "שכר הדירה הוא 5,000 ₪ לחודש [V1].\nהנכס פנוי."
    ws = _silent_period()
    a, r = _report(md, ws)
    assert r.edit(a).doc.text.startswith("שכר הדירה הוא 5,000 ₪ [V1].\nהנכס פנוי.")


def test_a_table_row_loses_the_unsupported_period_inside_its_cell():
    md = "| פריט | סכום |\n|---|---|\n| שכר דירה | 5,000 ₪ לחודש [V1] |"
    a, r = _report(md, _silent_period())
    assert r.removed_units() == set()
    assert r.edit(a).doc.text.startswith("| פריט | סכום |\n|---|---|\n| שכר דירה | 5,000 ₪ [V1] |")


# --- the edited unit gets a fresh verdict, and an unsupported verdict removes it ---------------------------------------------

@pytest.fixture(autouse=True)
def _no_ledger(monkeypatch):
    """The coverage ledger reads the database; the final check here is about the edited sentence."""
    monkeypatch.setattr(coverage, "build", lambda ws, answer, question: ({}, answer))


def _final(markdown: str, ws, rule):
    """The answer through verification and the final check, with the judge ``rule(text)`` answering the final check."""
    a = _answer(markdown)
    r = verify_answer(_judge(lambda t: "supported"), a, ws, "?", [])
    seen: list[str] = []
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        seen.append(input)
        return {"verdicts": [{"index": int(i), "verdict": rule(t), "reason": "בדיקה"}
                             for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    usage: list = []
    done = final_check.finalize(p, ws, a, r, TurnRequirements(), question="?", usage=usage)
    return done, r, seen, usage


def test_the_edited_sentence_is_judged_again_in_the_final_check_as_it_will_be_saved():
    done, r, seen, usage = _final("שכר הדירה הוא 5,000 ₪ לחודש [V1].", _silent_period(), lambda t: "supported")
    assert [u["purpose"] for u in usage] == [final_check.FINAL_LABEL]
    assert len(seen) == 1 and "5,000 ₪" in seen[0] and "לחודש" not in seen[0]
    assert done.composition.answer.answer_markdown.startswith("שכר הדירה הוא 5,000 ₪ [V1].")
    assert r.removed_units() == set()


def test_an_unsupported_final_verdict_removes_the_edited_unit():
    done, r, seen, usage = _final("שכר הדירה הוא 5,000 ₪ לחודש [V1].\nהנכס פנוי [V1].", _silent_period(),
                                  lambda t: "unsupported" if "5,000" in t else "supported")
    text = done.composition.answer.answer_markdown
    assert "5,000" not in text.split("\n\n> ")[0]
    assert r.removed_units() == {0} and r.counts()["edited"] == 0  # removed after the edit: a removal, not an edit
    assert r.removals()[0].check == "final"


def test_the_marker_form_is_judged_again_too():
    a = _answer("שכר הדירה הוא 5,000 ₪ לחודש [V1].")
    ws = _silent_period()
    r = verify_answer(_judge(lambda t: "supported"), a, ws, "מה שכר הדירה לחודש?", [])
    usage: list = []
    p = _judge(lambda t: "supported")
    done = final_check.finalize(p, ws, a, r, TurnRequirements(), question="מה שכר הדירה לחודש?", usage=usage)
    assert [u["purpose"] for u in usage] == [final_check.FINAL_LABEL]
    assert "(התקופה לא צוינה במקור)" in done.composition.answer.answer_markdown


def test_a_report_edits_the_same_text_every_time_it_is_composed():
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש [V1].", _silent_period())
    assert r.edit(a).doc.text == r.edit(a).doc.text


# --- a derived number that does not verify is cut with its clause, the grounded number stays (round 13 U9, KTD9, R12, AE5) --------

def _coefficient(written: str = "0.8"):
    return _registered(written, f"מקדם הדגמה {written}", unit="ratio")


def test_a_reduction_that_is_not_one_minus_the_cited_coefficient_is_cut_to_its_clause_and_the_coefficient_stays():
    a, r = _report("מקדם 0.8 — הפחתה של 25% [V1].", _coefficient())
    assert r.removed_units() == set() and r.removals() == [] and r.failed_units() == set()
    assert [(p.check, p.resolution) for p in r.problems] == [("unstated_number", "qualifier")]
    text = r.edit(a).doc.text
    assert text.startswith("מקדם 0.8 [V1].")
    for stub in ("הפחתה", "25", "של", "—"):
        assert stub not in text.split("\n")[0], text


def test_the_number_edit_is_counted_as_edited_and_the_note_says_a_derived_number_went():
    a, r = _report("מקדם 0.8 — הפחתה של 25% [V1].", _coefficient())
    counts = r.counts()
    assert counts["removed"] == 0 and counts["edited"] == 1 and counts["removals"] == []
    note = r.apply(a).answer_markdown.split("\n\n", 1)[1]
    assert note.startswith(">") and EDIT_NOTE in note and REMOVAL_NOTE not in note
    assert [row.action for row in r.logged()] == ["qualified"]


def test_the_number_edit_waits_for_the_repair_rounds_like_every_qualifier_edit():
    a, r = _report("מקדם 0.8 — הפחתה של 25% [V1].", _coefficient())
    assert not r.ok and r.repair_keys() and "25" in r.problems_text()


@pytest.mark.parametrize("raw, kept", [
    ("מקדם 0.8, בהפחתה של 25% [V1].", "מקדם 0.8 [V1]."),
    ("מקדם 0.8 (הפחתה של 25%) [V1].", "מקדם 0.8 [V1]."),
    ("מקדם 0.8 [V1] — הפחתה של 25%.", "מקדם 0.8 [V1]."),
    ("מקדם 0.8 — הפחתה של 25%, והנכס פנוי [V1].", "מקדם 0.8, והנכס פנוי [V1]."),
])
def test_the_cut_ends_at_the_clause_boundary_in_each_written_form(raw, kept):
    a, r = _report(raw, _coefficient())
    assert r.removed_units() == set()
    assert r.edit(a).doc.text.startswith(kept), r.edit(a).doc.text


def test_two_derived_clauses_of_one_unit_are_both_cut():
    ws = _coefficient()
    a, r = _report("מקדם 0.8 — הפחתה של 25% [V1], ושוב מקדם 0.8 — הפחתה של 30% [V1].", ws)
    text = r.edit(a).doc.text.split("\n")[0]
    assert "25" not in text and "30" not in text and "הפחתה" not in text and text.count("0.8") == 2


def test_a_derived_clause_that_touches_a_citation_or_a_table_cell_edge_keeps_the_removal_path():
    a, r = _report("מקדם 0.8 — הפחתה של [V1] 25%.", _coefficient())  # the citation sits inside the clause
    assert r.removed_units() == {0} and r.counts()["edited"] == 0
    md = "| מקדם | הערה |\n|---|---|\n| 0.8 [V1] | הפחתה של 25% |"
    a, r = _report(md, _coefficient())
    assert r.counts()["edited"] == 0


def test_a_unit_that_is_only_the_unstated_number_keeps_the_existing_removal():
    a, r = _report("הפחתה של 25% [V1].", _coefficient())
    assert r.removed_units() == {0} and r.counts()["edited"] == 0 and r.removals()[0].check == "unstated_number"


def test_an_unstated_number_with_no_derivation_wording_is_not_a_number_edit():
    a, r = _report("מקדם 0.8, ושטח של 25 מ״ר [V1].", _coefficient())
    assert r.counts()["edited"] == 0  # the judge splits it and a cut marks it, as before


def test_the_number_edit_label_exists_beside_the_qualifier_labels():
    from app.chat.verify import QUALIFIER_LABELS

    assert {"period", "basis", "vat", "number"} <= set(QUALIFIER_LABELS)


def test_the_edited_derived_sentence_is_judged_again_in_the_final_check():
    done, r, seen, usage = _final("מקדם 0.8 — הפחתה של 25% [V1].", _coefficient(), lambda t: "supported")
    assert [u["purpose"] for u in usage] == [final_check.FINAL_LABEL]
    assert len(seen) == 1 and "0.8" in seen[0] and "25" not in seen[0]
    assert done.composition.answer.answer_markdown.startswith("מקדם 0.8 [V1].")
