"""The qualifier edit (round 12 U10, KTD10, R7, AE5): a number the sources back survives when only a qualifier the answer
gives it — a period, an area basis, a VAT status — is one its source does not state. The edit runs after the repair rounds
and before claim removal; it removes the qualifier's own words (or, when the question asked about that attribute, writes
that the source does not state it), is recorded in the removal note as an edit and not a removal, and the edited sentence
is judged again in the final check. A source that states a different value, or a number that fails its own check, keeps
the removal path. The judge is scripted; synthetic text only."""

from __future__ import annotations

import re
import uuid

import pytest

from app.chat import coverage, final_check, verify
from app.chat.verify import Expansion, TurnRequirements, verify_answer
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider
from tests.unit.test_verify_reread import FakeFetcher
from tests.unit.test_verify_units import (
    AREA,
    LEAD_IN,
    LEAD_SOURCE,
    MARKER_1,
    _answer,
    _claims_judge,
    _judge,
    _registered,
    _ws,
)

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


# --- the table's notes are read before a qualifier is edited out (round 13 U10, KTD11, R14, AE6) ---------------------------------

def _noted(expansions: dict, vat: str = "excluded", table_text: str = "שווי הנכס | 2,450,000 ₪", **kw):
    """A workspace whose V1 was taken from the table S1 with a VAT status only the model asserted, and a fetcher that gives
    the table's context (``expansions``, by kind)."""
    ws = _registered("2,450,000", "שווי הנכס: 2,450,000 ₪", vat=vat, provenance={"vat": "model_asserted"}, **kw)
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומת הדגמה", section="טבלה", location="טבלה 3",
                  kind="table", text=table_text, table_index=0)
    return ws, FakeFetcher({("S1", k): Expansion(v, "הקשר") for k, v in expansions.items()}, ws=ws)


VAT_UNIT = "שווי הנכס הוא 2,450,000 ₪, לא כולל מע\"מ [V1]."
NOTES_EXCL = "הערה: כל הסכומים בטבלה אינם כוללים מע\"מ."


def _read(markdown: str, ws, fetcher, question: str = "?"):
    a = _answer(markdown)
    return a, verify_answer(_judge(lambda t: "supported"), a, ws, question, [], reread=fetcher)


def test_a_vat_status_the_tables_notes_state_is_kept_with_its_anchor_and_the_edit_is_skipped():
    ws, fetcher = _noted({"table_headers": NOTES_EXCL})
    a, r = _read(VAT_UNIT, ws, fetcher)
    assert r.qualifier_edits == {} and r.removed_units() == set() and r.counts()["edited"] == 0
    assert not any(p.check in ("vat", "meaning") for p in r.problems)
    (cite,) = [p.cite for p in r.problems if p.kind == "needs_citation"]
    assert cite == "S2" and cite in ws.sources  # the notes, registered where they were read
    assert (ws.sources[cite].table_index, ws.sources[cite].document_id) == (0, ws.sources["S1"].document_id)
    out = r.apply(a).answer_markdown
    assert "לא כולל מע\"מ" in out and f"[V1][{cite}]" in out
    (log,) = r.claims[0][0].rereads
    assert (log["source"], log["outcome"], log["expanded_source"]) == ("S1", "found", cite)
    assert fetcher.calls[0] == ("S1", "table_headers")


@pytest.mark.parametrize("kind", ["footnotes", "section_lead", "section_notes", "next_page"])
def test_the_other_places_a_qualifier_may_be_stated_are_read_too(kind):
    ws, fetcher = _noted({kind: NOTES_EXCL})
    _, r = _read(VAT_UNIT, ws, fetcher)
    assert r.qualifier_edits == {} and r.removed_units() == set()
    assert {c[1] for c in fetcher.calls} == set(verify.QUALIFIER_READS)


def test_notes_that_say_nothing_of_the_vat_leave_the_edit_as_before():
    ws, fetcher = _noted({"table_headers": "הערה: הסכומים באלפי ₪."})
    a, r = _read(VAT_UNIT, ws, fetcher)
    assert r.removed_units() == set() and r.counts()["edited"] == 1
    assert r.edit(a).doc.text.startswith("שווי הנכס הוא 2,450,000 ₪ [V1].")
    assert list(ws.sources) == ["S1"]  # nothing was registered
    assert r.claims[0][0].rereads[0]["outcome"] == "not_found"


def test_notes_that_state_the_other_vat_do_not_back_the_claim():
    ws, fetcher = _noted({"table_headers": "הערה: כל הסכומים בטבלה כוללים מע\"מ."})
    _, r = _read(VAT_UNIT, ws, fetcher)
    assert list(ws.sources) == ["S1"] and not any(p.kind == "needs_citation" for p in r.problems)
    assert r.removed_units() == set() and r.counts()["edited"] == 1  # the wrong status still goes, as before


def test_nothing_to_read_leaves_the_edit_and_logs_it():
    ws, fetcher = _noted({})
    _, r = _read(VAT_UNIT, ws, fetcher)
    assert r.counts()["edited"] == 1 and r.claims[0][0].rereads[0]["outcome"] == "no_context"


def test_a_failing_read_leaves_the_edit_and_is_not_a_verdict():
    ws, _ = _noted({})
    _, r = _read(VAT_UNIT, ws, FakeFetcher(fail=True, ws=ws))
    assert r.counts()["edited"] == 1 and r.claims[0][0].rereads[0]["outcome"] == "check_failed"


def test_the_read_counts_against_the_turns_bound(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_reread_claims", 0)
    ws, fetcher = _noted({"table_headers": NOTES_EXCL})
    _, r = _read(VAT_UNIT, ws, fetcher)
    assert fetcher.calls == [] and r.counts()["edited"] == 1
    assert r.claims[0][0].rereads[0]["outcome"] == "bound"


def test_no_read_starts_without_time():
    import time

    ws, fetcher = _noted({"table_headers": NOTES_EXCL})
    a = _answer(VAT_UNIT)
    r = verify_answer(_judge(lambda t: "supported"), a, ws, "?", [], deadline=time.monotonic() + verify.REREAD_MIN_SECONDS / 2,
                      reread=fetcher)
    assert fetcher.calls == [] and r.counts()["edited"] == 1
    assert r.claims[0][0].rereads[0]["outcome"] == "no_time"


def _rent(expansions: dict):
    """V1 = 5,000, taken from a table row whose own words state neither a period nor an area basis."""
    from decimal import Decimal

    ws, fetcher = _noted(expansions, table_text="שכר דירה | 5,000 ₪", unit="ILS")
    ws.values["V1"].written, ws.values["V1"].value = "5,000", Decimal(5000)
    return ws, fetcher


@pytest.mark.parametrize("unit_md, note", [
    ("דמי השכירות הם 5,000 ₪ ברוטו [V1].", "הערה: השטחים בטבלה ברוטו."),
    ("שכר הדירה הוא 5,000 ₪ לחודש [V1].", "כל הסכומים בטבלה הם לחודש."),
])
def test_a_basis_or_a_period_the_notes_state_is_kept_and_without_them_it_is_edited(unit_md, note):
    ws, fetcher = _rent({})
    _, r = _read(unit_md, ws, fetcher)
    assert r.counts()["edited"] == 1  # silent sources: the edit, as before
    ws, fetcher = _rent({"table_headers": note})
    a, r = _read(unit_md, ws, fetcher)
    assert r.counts()["edited"] == 0 and r.qualifier_edits == {} and r.removed_units() == set()
    assert r.apply(a).answer_markdown.startswith(unit_md[:-len(" [V1].")])
    assert [p.cite for p in r.problems if p.kind == "needs_citation"] == ["S2"]


def test_the_read_does_not_run_for_a_unit_that_would_not_be_edited():
    ws, fetcher = _noted({"table_headers": NOTES_EXCL})
    _, r = _read("שווי הנכס הוא 7,000,000 ₪, לא כולל מע\"מ [V1].", ws, fetcher)  # a number that fails its own check
    assert ("S1", "table_headers") not in fetcher.calls and r.removed_units() == {0}  # (the unstated number's own read ran)


def test_a_round_that_follows_reuses_the_notes_already_read(monkeypatch):
    ws, fetcher = _noted({"table_headers": NOTES_EXCL})
    _read(VAT_UNIT, ws, fetcher)
    n = len(fetcher.calls)
    a, r = _read(VAT_UNIT, ws, fetcher)
    assert len(fetcher.calls) == n and r.qualifier_edits == {} and r.removed_units() == set()


# --- coherence after a cut (round 13 U11, KTD13, R16, AE7) -------------------------------------------------------------------

REDUCTION_SOURCE = "מקדם הפחתה 20% נקבע לחלקה. השטח העיקרי בתא שטח 1 הוא 1,200 מ״ר."
STUB = "הפחתה של"  # the claim the judge fails; the standing claim "20%" is left with no word of its own
STUB_UNIT = f"{STUB} 20% [S1]."


def _stub_judge():
    """A judge that splits the stub unit into a failing "הפחתה של" and a standing "20%"; every other unit is supported."""
    return _claims_judge(lambda t: [(STUB, "unsupported", {"failure": "absent_from_source"}), ("20%", "supported")]
                         if STUB in t else None)


def test_a_cut_that_leaves_no_word_is_a_whole_unit_removal_with_no_marker_and_the_answer_opens_with_substance():
    a = _answer(f"{STUB_UNIT}\n{AREA} [S1].")
    r = verify_answer(_stub_judge(), a, _ws(REDUCTION_SOURCE), "?", [])
    assert r.cuts == {} and r.removed_units() == {0} and r.ok
    (p,) = r.removals()
    assert p.check == "dependency" and p.failure_kind == "absent_from_source" and p.basis and p.claim_id
    text = r.edit(a).doc.text
    assert text.startswith(f"{AREA} [S1].") and "חלק שלא אומת הושמט" not in text and "20%" not in text
    assert {x.action for x in r.claims[0]} == {"removed"} and r.counts()["removed"] == 1


def test_no_bullet_or_paragraph_made_only_of_markers_remains_and_its_lead_in_goes_with_it():
    a = _answer(f"{LEAD_IN}\n- {STUB_UNIT}\n- {STUB_UNIT}\n{AREA} [S1].")
    r = verify_answer(_stub_judge(), a, _ws(f"{REDUCTION_SOURCE} {LEAD_SOURCE}"), "?", [])
    assert r.removed_units() == {0, 1, 2}
    assert [p.check for p in r.removals()] == ["dependency"] * 3
    text = r.edit(a).doc.text
    assert text.startswith(f"{AREA} [S1].") and "חלק שלא אומת הושמט" not in text and LEAD_IN not in text


def test_a_cut_that_leaves_words_is_still_a_marker():
    a = _answer(f"{AREA}, {STUB} 20% [S1].")
    r = verify_answer(_claims_judge(lambda t: [(AREA, "supported"), ("20%", "unsupported")] if STUB in t else None), a,
                      _ws(REDUCTION_SOURCE), "?", [])
    assert r.removed_units() == set() and r.edit(a).doc.text.startswith(f"{AREA}, {STUB} {MARKER_1} [S1].")


def test_a_cut_ending_in_a_connector_is_extended_to_the_clause_boundary():
    """The judge's span ends in "של": the object of the connector goes with it, up to the clause end (the citation and the
    full stop stay)."""
    a = _answer(f"{AREA}, {STUB} 20% [S1].")
    r = verify_answer(_claims_judge(lambda t: [(AREA, "supported"), (STUB, "unsupported")] if STUB in t else None), a,
                      _ws(REDUCTION_SOURCE), "?", [])
    assert r.removed_units() == set()
    assert r.edit(a).doc.text.startswith(f"{AREA}, {MARKER_1} [S1].")


def test_an_extension_that_would_take_a_standing_claim_is_not_made():
    a = _answer(f"{STUB} 20% [S1]. {AREA} [S1].")
    r = verify_answer(_claims_judge(lambda t: [(STUB, "unsupported"), ("20%", "supported")] if STUB in t else None), a,
                      _ws(REDUCTION_SOURCE), "?", [])
    # nothing of the unit would stand once the cut leaves "20%" alone: it is removed whole, not cut across a claim
    assert r.cuts == {} and r.removed_units() == {0}


def test_recomposition_after_the_rules_reproduces_the_text_and_the_registry():
    a = _answer(f"{LEAD_IN}\n- {STUB_UNIT}\n{AREA}, {STUB} 20% [S1].")
    r = verify_answer(_claims_judge(lambda t: [(AREA, "supported"), (STUB, "unsupported")] if AREA in t else (
        [(STUB, "unsupported", {"failure": "absent_from_source"}), ("20%", "supported")] if STUB in t else None)),
        a, _ws(REDUCTION_SOURCE), "?", [])
    first, second = r.edit(a), r.edit(a)
    assert first.doc.text == second.doc.text and first.doc.spans() == second.doc.spans()


@pytest.mark.parametrize("lead", ["הנתון נקבע כי", "ההפחתה היא של", "המקדם אשר", "השיעור —", "כולל:", "המקדם ו־"])
def test_every_connector_extends_the_cut_to_the_clause_end(lead):
    a = _answer(f"{AREA}, {lead} 20% במסגרת השומה [S1]. השורה הבאה.")
    r = verify_answer(_claims_judge(lambda t: [(AREA, "supported"), (lead, "unsupported")] if lead in t else None), a,
                      _ws(REDUCTION_SOURCE, "השורה הבאה."), "?", [])
    assert r.removed_units() == set()
    assert r.edit(a).doc.text.startswith(f"{AREA}, {MARKER_1} [S1]. השורה הבאה.")


def test_a_decimal_point_or_thousands_comma_does_not_end_the_clause():
    a = _answer(f"{AREA}, {STUB} 0.8 או 1,200 מ״ר [S1].")
    r = verify_answer(_claims_judge(lambda t: [(AREA, "supported"), (STUB, "unsupported")] if STUB in t else None), a,
                      _ws("0.8 1,200 " + REDUCTION_SOURCE), "?", [])
    assert r.edit(a).doc.text.startswith(f"{AREA}, {MARKER_1} [S1].")
