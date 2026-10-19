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
from app.chat.evidence import (
    TABLE_AFTER,
    TABLE_BEFORE,
    TABLE_BETWEEN,
    TABLE_HEADING_LIKE,
    TABLE_NO_REFERENCE,
    TABLE_NOT_ADJACENT,
    TABLE_PATH_PREFIX,
)
from app.chat.tools import Workspace
from app.chat.verify import Expansion, TurnRequirements, split_units, verify_answer
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
    a, r = _report("שכר הדירה הוא 5,000 ₪ פלדלת לחודש [V1].", ws)
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
    a, r = _report("שכר הדירה הוא 5,000 ₪ פלדלת לחודש [V1].", ws)
    assert r.removed_units() == set()
    assert r.edit(a).doc.text.startswith("שכר הדירה הוא 5,000 ₪ [V1].")


# --- what the edit never touches ------------------------------------------------------------------------------------------

def test_a_source_that_states_another_period_keeps_the_removal_path():
    ws = _registered("5,000", RENT_YEARLY, period="year")
    a, r = _report("שכר הדירה הוא 5,000 ₪ לחודש [V1].", ws)
    assert r.removed_units() == {0} and r.counts()["edited"] == 0
    assert r.removals()[0].check == "meaning" and "5,000" not in r.apply(a).answer_markdown.split("\n\n")[0]


@pytest.mark.parametrize("source_vat", ["לא כולל מ.ע.מ.", "אינו כולל מס ערך מוסף", "אינו כולל מעמ"])
def test_a_vat_the_source_writes_in_another_form_backs_the_claim_with_no_edit(source_vat):
    """Round 13 V2/W5/W10: the claim writes "ללא מע״מ", the cited passage writes the same fact as "מ.ע.מ." or in words: no
    `vat` problem and no qualifier edit (synthetic text)."""
    ws = _ws(f"שווי הנכס לעיל הוא 2,450,000 ₪ וזה {source_vat}.")
    a, r = _report("שווי הנכס הוא 2,450,000 ₪ ללא מע״מ [S1].", ws)
    assert not any(p.check == "vat" for p in r.problems)
    assert r.qualifier_edits == {} and r.removed_units() == set() and r.counts()["edited"] == 0
    assert "ללא מע״מ" in r.apply(a).answer_markdown


def test_a_registered_value_cited_as_v_reads_the_vat_its_own_source_passage_writes():
    """The claim cites V1 only; V1's quote is a bare number and its VAT was the model's, but the passage V1 was taken from
    (``source_id``) writes the VAT in another form: that passage backs the claim."""
    ws = _registered("2,450,000", "2,450,000", vat="excluded", provenance={"vat": "model_asserted"})
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומת הדגמה", section="סעיף 3", location="סעיף 3",
                  kind="paragraph", text="השווי לעיל 2,450,000 ₪ אינו כולל מ.ע.מ.")
    a, r = _report("שווי הנכס הוא 2,450,000 ₪ ללא מע״מ [V1].", ws)
    assert not any(p.check == "vat" for p in r.problems) and r.qualifier_edits == {}


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
    ("דמי השכירות הם 5,000 ₪ פלדלת [V1].", "הערה: השטחים בטבלה פלדלת."),
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


# --- the scope of a value: added from the source, never cut, never a reason to remove (round 17 U5, KTD5; R10-R13; AE5, AE6) ---------

SCOPE = "גישת היוון ההכנסות בלבד"
SCOPE_NOTE = f"הטבלה שלעיל מתייחסת ל{SCOPE}, ואינה משקפת את שווי הנכס בגישת ההשוואה."
SCOPE_ROWS = "שיעור היוון | שווי הנכס (אלפי ₪)\n5.5% | 12,400\n6.0% | 11,800\n6.5% | 11,300"
SCOPE_UNIT = "בשיעור היוון 6.0% שווי הנכס הוא 11,800 אלפי ₪ [S1]."
SCOPE_ADDED = f"(היקף הערך לפי המקור: {SCOPE})"
SCOPE_WHERE = "טבלה 5.1 בעמוד 12"


def _table(*, heading: str = "5. ניתוח רגישות לשיעור ההיוון", before: str | None = "הטבלה שלהלן מציגה את שווי הנכס.",
           caption: str = "רגישות השווי לשיעור ההיוון", rows: str = SCOPE_ROWS, notes: tuple = (),
           after: str | None = SCOPE_NOTE, between: bool = False, marks: tuple = (), label: str | None = None) -> str:
    """A table read as the verifier holds it (round 17 U4, round 18 U4): the heading path, the heading-like label before it (``label``),
    the block before, the caption and size lines, the rows, the table's notes, and the block after (tagged between tables when
    ``between``, and with ``marks``: the reasons it is not attributed)."""
    lines = [f"{TABLE_PATH_PREFIX} {heading}"] if heading else []
    lines += [f"[{TABLE_BEFORE} · {TABLE_HEADING_LIKE} · פסקה · בלוק 38] {label}"] if label else []
    lines += [f"[{TABLE_BEFORE} · פסקה · בלוק 40] {before}"] if before else []
    lines += [caption, "הטבלה: 3 שורות", rows, *notes]
    mid = "".join(f" · {m}" for m in [*([TABLE_BETWEEN] if between else []), *marks])
    lines += [f"[{TABLE_AFTER}{mid} · פסקה · בלוק 47] {after}"] if after else []
    return "\n".join(lines)


def _tables(*texts: str) -> Workspace:
    """S1, S2 ... : table reads of one invented document, each with its own location."""
    ws = Workspace(ctx=None)
    for text in texts:
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="שומת הדגמה", section=None,
                      location=SCOPE_WHERE, kind="table", text=text, table_index=0)
    return ws


def _scope_judge(scope: str = SCOPE, *, verdict: str = "supported", defect: str = "scope", seen: list | None = None):
    """A judge that finds every unit presented wider than its source's scope (``defect``) and names ``scope`` as the omitted phrase."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        return {"verdicts": [{"index": int(i), "verdict": verdict, "reason": "בדיקה", "defect": defect, "scope": scope}
                             for i in re.findall(r'<unit index="(\d+)"', input)]}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _scoped(markdown: str, ws, judge, question: str = "?"):
    a = _answer(markdown)
    return a, verify_answer(judge, a, ws, question, [])


def _span_kinds(edited) -> list[str]:
    return [s.kind for s in edited.doc.spans()]


def test_ae5_a_table_value_written_as_the_total_gets_the_scope_its_following_paragraph_states_with_its_citation():
    a, r = _scoped(SCOPE_UNIT, _tables(_table()), _scope_judge())
    assert r.removed_units() == set() and r.counts()["removed"] == 0 and r.ok  # no repair round, no removal
    (p,) = [x for x in r.problems if x.check == verify.SCOPE_CHECK]
    assert p.annotatable and p.cite == "S1" and p.annotation == SCOPE and p.number == "11,800" and not p.repairable
    out = r.edit(a)
    first = out.doc.text.split("\n")[0]
    assert first == f"בשיעור היוון 6.0% שווי הנכס הוא 11,800 אלפי ₪ {SCOPE_ADDED} [S1]."  # the sentence stays; the value is unchanged
    assert _span_kinds(out) == ["qualifier"]
    # a scope written from the source is counted in ``scoped``; ``annotated`` stays the qualifiers written as the source wrote them
    assert r.counts()["annotated"] == 0 and r.counts()["scoped"] == 1 and [row.action for row in r.logged()] == ["qualified"]


def test_the_scope_is_written_with_its_citation_when_the_sentence_cites_the_value_not_the_table():
    ws = _tables(_table())
    from decimal import Decimal

    from app.chat import calc

    ws.values["V1"] = calc.Value("V1", Decimal(11800), "11,800", "S1", uuid.uuid4(), uuid.uuid4(), None, "שומת הדגמה", "סעיף 5",
                                 "שווי הנכס", "value", "ILS", "none", "not_stated", "", "נכס הדגמה", "appraiser_determination",
                                 {"unit": "source", "vat": "source", "area_basis": "source", "period": "source"},
                                 {"quote": "6.0% | 11,800"}, "6.0% | 11,800")
    a, r = _scoped("שווי הנכס בשיעור היוון 6.0% הוא 11,800 אלפי ₪ [V1].", ws, _scope_judge())
    assert r.removed_units() == set()
    assert r.edit(a).doc.text.split("\n")[0] == f"שווי הנכס בשיעור היוון 6.0% הוא 11,800 אלפי ₪ {SCOPE_ADDED[:-1]} [S1]) [V1]."


def test_the_answer_that_already_writes_the_scope_keeps_it_and_nothing_is_added():
    unit = f"בשיעור היוון 6.0% שווי הנכס הוא 11,800 אלפי ₪ לפי {SCOPE} [S1]."
    a, r = _scoped(unit, _tables(_table()), _scope_judge())
    assert r.problems == [] and r.removed_units() == set()
    assert r.apply(a).answer_markdown == unit


def test_a_scope_in_the_section_heading_binds_to_a_value_of_a_table_under_it():
    ws = _tables(_table(heading=f"5. ניתוח רגישות ב{SCOPE}", after=None, before=None))
    a, r = _scoped(SCOPE_UNIT, ws, _scope_judge())
    assert r.edit(a).doc.text.split("\n")[0].count(SCOPE_ADDED) == 1 and r.removed_units() == set()


def test_a_scope_in_the_sentence_before_the_table_binds_to_the_table_it_precedes():
    ws = _tables(_table(before=f"הנתונים שלהלן הם ב{SCOPE}.", after=None))
    a, r = _scoped(SCOPE_UNIT, ws, _scope_judge())
    assert r.edit(a).doc.text.split("\n")[0].count(SCOPE_ADDED) == 1 and r.removed_units() == set()


@pytest.mark.parametrize("place", ["caption", "note"])
def test_a_scope_in_the_tables_caption_or_notes_binds_to_its_values(place):
    ws = _tables(_table(caption=f"רגישות השווי ב{SCOPE}", after=None, before=None) if place == "caption"
                 else _table(notes=(f"הערה: הנתונים ב{SCOPE}.",), after=None, before=None))
    a, r = _scoped(SCOPE_UNIT, ws, _scope_judge())
    assert r.edit(a).doc.text.split("\n")[0].count(SCOPE_ADDED) == 1 and r.removed_units() == set()


def test_a_scope_phrase_from_another_tables_note_is_rejected_and_the_value_keeps_a_limitation_note():
    """R12: the trailing note of table A is never applied to a value cited from table B — also when it is offered for B."""
    other = _table(heading="6. ניתוח רגישות לדמי השכירות", before=None, after="הנתונים בטבלה זו מתייחסים לכל שלבי הפרויקט.")
    ws = _tables(_table(), other)
    a, r = _scoped(SCOPE_UNIT.replace("[S1]", "[S2]"), ws, _scope_judge())
    assert [p.check for p in r.problems] == [verify.SCOPE_LIMIT_CHECK] and r.removed_units() == set()
    out = r.edit(a)
    assert SCOPE not in out.doc.text and _span_kinds(out) == ["scope_note"]
    assert f"(היקף הערך במקור לא אומת; הערך נקרא ב-S2 ({SCOPE_WHERE}))" in out.doc.text.split("\n")[0]
    assert r.counts()["annotated"] == 0 and r.counts()["scoped"] == 1  # the note that the scope was not verified is a scope count too
    # the same offer for the table it belongs to is accepted
    a1, r1 = _scoped(SCOPE_UNIT, ws, _scope_judge())
    assert SCOPE_ADDED in r1.edit(a1).doc.text


def test_a_value_whose_sources_do_not_all_state_the_scope_is_not_given_it():
    """The sentence cites both tables and states a number both hold: the phrase must stand in each one's own context."""
    ws = _tables(_table(), _table(after=None))
    a, r = _scoped(SCOPE_UNIT.replace("[S1]", "[S1][S2]"), ws, _scope_judge())
    assert [p.check for p in r.problems] == [verify.SCOPE_LIMIT_CHECK]
    assert SCOPE not in r.edit(a).doc.text


def test_ae6_a_scope_the_judge_names_that_no_context_part_states_keeps_the_value_with_a_limitation_note():
    a, r = _scoped(SCOPE_UNIT, _tables(_table()), _scope_judge("רק במסגרת תרחיש הפיתוח המלא"))
    assert r.removed_units() == set() and r.counts()["removed"] == 0
    (p,) = r.problems
    assert p.check == verify.SCOPE_LIMIT_CHECK and p.annotatable and not p.repairable
    out = r.edit(a)
    first = out.doc.text.split("\n")[0]
    assert first == f"בשיעור היוון 6.0% שווי הנכס הוא 11,800 אלפי ₪ (היקף הערך במקור לא אומת; הערך נקרא ב-S1 ({SCOPE_WHERE})) [S1]."
    assert _span_kinds(out) == ["scope_note"] and "תרחיש" not in first


@pytest.mark.parametrize("phrase", ["", "בלבד", "גישה אחרת לגמרי שאין לה זכר במקור"])
def test_a_judge_that_names_no_usable_phrase_gets_the_limitation_note_too(phrase):
    a, r = _scoped(SCOPE_UNIT, _tables(_table()), _scope_judge(phrase))
    assert [p.check for p in r.problems] == [verify.SCOPE_LIMIT_CHECK] and r.removed_units() == set()
    assert "לא אומת" in r.edit(a).doc.text


def test_a_phrase_that_stands_only_in_a_paragraph_between_two_tables_is_never_asserted_as_the_values_scope():
    a, r = _scoped(SCOPE_UNIT, _tables(_table(between=True)), _scope_judge())
    assert [p.check for p in r.problems] == [verify.SCOPE_LIMIT_CHECK] and r.removed_units() == set()
    first = r.edit(a).doc.text.split("\n")[0]
    assert SCOPE not in first and "משותפת לשתי טבלאות" in first and "הערך נקרא ב-S1" in first


def test_a_phrase_from_a_paragraph_with_other_blocks_between_it_and_the_table_is_not_asserted_and_the_note_says_why():
    """AE7: the same paragraph with an unrelated block between it and the table is not attributed."""
    a, r = _scoped(SCOPE_UNIT, _tables(_table(marks=(TABLE_NOT_ADJACENT,))), _scope_judge())
    assert [p.check for p in r.problems] == [verify.SCOPE_LIMIT_CHECK] and r.removed_units() == set()
    first = r.edit(a).doc.text.split("\n")[0]
    assert SCOPE not in first and "בין הפסקה לטבלה יש בלוקים אחרים" in first and "משותפת לשתי טבלאות" not in first
    assert f"הערך נקרא ב-S1 ({SCOPE_WHERE})" in first


def test_a_phrase_from_a_paragraph_that_does_not_point_to_the_table_is_not_asserted_and_the_note_says_why():
    """AE7: the same paragraph without the reference word is not attributed, whether or not it lies between two tables."""
    for between in (False, True):
        a, r = _scoped(SCOPE_UNIT, _tables(_table(marks=(TABLE_NO_REFERENCE,), between=between)), _scope_judge())
        assert [p.check for p in r.problems] == [verify.SCOPE_LIMIT_CHECK] and r.removed_units() == set()
        first = r.edit(a).doc.text.split("\n")[0]
        assert SCOPE not in first and "אינה מפנה אליה במילותיה" in first and "משותפת לשתי טבלאות" not in first


def test_the_note_names_the_adjacency_before_the_wording_when_a_paragraph_lacks_both():
    a, r = _scoped(SCOPE_UNIT, _tables(_table(marks=(TABLE_NOT_ADJACENT, TABLE_NO_REFERENCE), between=True)), _scope_judge())
    first = r.edit(a).doc.text.split("\n")[0]
    assert "בין הפסקה לטבלה יש בלוקים אחרים" in first and "אינה מפנה" not in first


def test_a_value_of_two_cited_tables_is_noted_for_the_reason_the_paragraph_of_each_fails():
    ws = _tables(_table(marks=(TABLE_NO_REFERENCE,)), _table(marks=(TABLE_NOT_ADJACENT,)))
    a, r = _scoped(SCOPE_UNIT.replace("[S1]", "[S1][S2]"), ws, _scope_judge())
    assert [p.check for p in r.problems] == [verify.SCOPE_LIMIT_CHECK]
    assert "בין הפסקה לטבלה יש בלוקים אחרים" in r.edit(a).doc.text


def test_a_heading_like_label_before_the_table_is_the_tables_heading_scope_for_a_judge_that_names_it():
    method = "4. גישת היוון ההכנסות"
    ws = _tables(_table(heading="", label=method, before="הטבלה שלהלן מציגה את תוצאות החישוב.", after=None))
    a, r = _scoped(SCOPE_UNIT, ws, _scope_judge("גישת היוון ההכנסות"))
    assert [p.check for p in r.problems] == [verify.SCOPE_CHECK] and r.removed_units() == set()
    assert "(היקף הערך לפי המקור: גישת היוון ההכנסות)" in r.edit(a).doc.text
    parts, _ = verify.source_context(ws.sources["S1"].text, True)
    assert [(p.place, p.text) for p in parts][:2] == [("heading", method), ("before", "הטבלה שלהלן מציגה את תוצאות החישוב.")]


def test_the_marks_of_a_part_tag_are_read_back_as_the_reason_it_is_not_attributed():
    text = _table(label="4. גישת היוון", marks=(TABLE_NOT_ADJACENT, TABLE_NO_REFERENCE), between=True)
    parts, _ = verify.source_context(text, True)
    assert [(p.place, p.reason, p.between) for p in parts] == [
        ("heading", "", False), ("heading", "", False), ("before", "", False), ("caption", "", False),
        ("after", "not_adjacent", True)]
    plain = verify.source_context(_table(marks=(TABLE_NO_REFERENCE,)), True)[0]
    assert plain[-1].reason == "no_reference" and not plain[-1].between
    legacy = verify.source_context(_table(between=True), True)[0]
    assert legacy[-1].reason == "between" and legacy[-1].between


def test_the_structured_parts_of_a_table_read_decide_the_standing_not_a_tag_in_its_text():
    """KTD5: the evidence travels as fields of the source, so verification needs no database read and no tag parsing."""
    from app.chat import tools as T

    ws = _tables(_table())
    src = ws.sources["S1"]
    assert verify._scope_standing(SCOPE, src) == "ok"  # the tags carry no mark: attributed
    src.table_parts = [T.TablePart("after", "paragraph", 47, SCOPE_NOTE, False, 1, 1, 0, "back")]  # but one block lies between
    assert verify._scope_standing(SCOPE, src) == "not_adjacent"
    src.table_parts = [T.TablePart("after", "paragraph", 47, SCOPE_NOTE, False, 1, 0, 0, "back")]
    assert verify._scope_standing(SCOPE, src) == "ok"
    src.table_parts = [T.TablePart("after", "paragraph", 47, SCOPE_NOTE, False, 1, 0, 0, "")]
    assert verify._scope_standing(SCOPE, src) == "no_reference"


@pytest.mark.parametrize("raw, clean", [
    ("(גישת היוון ההכנסות בלבד)", "גישת היוון ההכנסות בלבד"),
    ("גישת היוון ההכנסות (לפי תקן 17)", "גישת היוון ההכנסות (לפי תקן 17)"),
    ("גישת היוון ההכנסות (לפי תקן 17", ""),
    ("גישת היוון ההכנסות לפי תקן 17)", ""),
    ("(גישת היוון ההכנסות", ""),
    ("[גישת היוון ההכנסות בלבד]", "גישת היוון ההכנסות בלבד"),
    ("גישת היוון [הכנסות בלבד", ""),
    ("«גישת היוון ההכנסות בלבד».", "גישת היוון ההכנסות בלבד"),
    ("גישת היוון ההכנסות בלבד, [S1].", "גישת היוון ההכנסות בלבד"),
])
def test_a_judge_phrase_cut_inside_a_parenthesis_is_returned_balanced_or_rejected(raw, clean):
    assert verify._clean_scope(raw) == clean


def test_a_phrase_from_a_between_tables_paragraph_loses_to_the_same_phrase_in_the_tables_own_note():
    ws = _tables(_table(notes=(f"הערה: הנתונים ב{SCOPE}.",), between=True))
    a, r = _scoped(SCOPE_UNIT, ws, _scope_judge())
    assert [p.check for p in r.problems] == [verify.SCOPE_CHECK] and SCOPE_ADDED in r.edit(a).doc.text


def test_a_judge_that_calls_the_unit_unsupported_for_its_scope_alone_does_not_remove_it():
    a, r = _scoped(SCOPE_UNIT, _tables(_table()), _scope_judge(verdict="unsupported"))
    assert r.removed_units() == set() and r.counts()["removed"] == 0 and r.verdicts == {0: "partial"}
    assert SCOPE_ADDED in r.edit(a).doc.text


def test_a_hebrew_scope_note_qualifies_its_own_tables_value_and_never_the_next_tables():
    first = _table(after="הטבלה שלעיל מתייחסת לשנת 2024 בלבד.", before=None)
    second = _table(heading="6. ניתוח רגישות לדמי השכירות", before=None, after="הנתונים בטבלה זו מתייחסים לשנת 2025 בלבד.")
    ws = _tables(first, second)
    a, r = _scoped(SCOPE_UNIT, ws, _scope_judge("לשנת 2024 בלבד"))
    assert "(היקף הערך לפי המקור: לשנת 2024 בלבד)" in r.edit(a).doc.text
    a, r = _scoped(SCOPE_UNIT.replace("[S1]", "[S2]"), ws, _scope_judge("לשנת 2024 בלבד"))
    assert [p.check for p in r.problems] == [verify.SCOPE_LIMIT_CHECK] and "2024" not in r.edit(a).doc.text.split("\n")[0]


def test_a_unit_that_cites_no_source_has_nowhere_to_say_where_the_value_was_read_and_is_not_given_a_note():
    ws = _tables(_table())
    unit = split_units("בשיעור היוון 6.0% שווי הנכס הוא 11,800 אלפי ₪.")[0]
    assert verify.scope_outcome(unit, SCOPE, ws) == (False, None)


def test_the_judge_policy_and_schema_carry_the_scope_defect():
    assert "scope" in verify.JudgeVerdict.model_fields["defect"].annotation.__args__
    assert verify.JudgeVerdict(index=0, verdict="supported", reason="x", defect="scope", scope=SCOPE).scope == SCOPE
    assert "scope (כלל 32)" in verify.JUDGE_POLICY and "(32)" in verify.JUDGE_POLICY
    assert verify.DEFECT_FAILURES["scope"] in verify.FAILURE_KINDS


def test_the_between_tables_tag_is_the_one_the_table_read_writes():
    assert verify.TABLE_BETWEEN == TABLE_BETWEEN


def test_the_context_of_a_table_read_is_parsed_by_position_and_by_the_servers_labels():
    text = _table(notes=("הערה: אלפי ₪.",), between=True, before="פתיח לטבלה.")
    parts, body = verify.source_context(text, True)
    assert [(p.place, p.between) for p in parts] == [("heading", False), ("before", False), ("caption", False), ("note", False),
                                                     ("after", True)]
    assert parts[0].text == "5. ניתוח רגישות לשיעור ההיוון" and parts[1].text == "פתיח לטבלה." and parts[4].text == SCOPE_NOTE
    assert body == [line for line in SCOPE_ROWS.splitlines()]  # the rows only: the size line is no context


def test_a_pointer_to_a_part_returned_before_and_the_what_follows_line_hold_no_text_of_their_own():
    text = "\n".join([f"[{TABLE_BEFORE} · פסקה · בלוק 40: כבר הוחזר ב-S3]", "כותרת", SCOPE_ROWS,
                      "[אחרי הטבלה: פסקה (בלוק 48) — להמשך הקריאה מהקטע הבא: read(cursor=K1)]"])
    parts, body = verify.source_context(text, True)
    assert [p.place for p in parts] == ["caption"] and body == SCOPE_ROWS.splitlines()


# --- a qualifier the source states anywhere in its context is never cut (R10); one it does not state is cut as before ----------------

RENT_ROWS = "סוג | שכר דירה\nמשרדים | 5,000 ₪"
RENT_UNIT = "שכר הדירה למשרדים הוא 5,000 ₪ לחודש [S1]."


def _rent_ws(**kw) -> Workspace:
    return _tables(_table(rows=RENT_ROWS, caption="שכר הדירה בנכס", **kw))


@pytest.mark.parametrize("where", ["after", "before", "heading", "caption", "note"])
def test_a_period_the_sources_context_states_is_never_cut_whichever_part_states_it(where):
    stated = "כל הסכומים בטבלה הם לחודש"
    # more than four lines around the rows (``meaning.TABLE_AROUND``) are no longer read as the table's own by the meaning check
    kw = {"after": "הטבלה מסתיימת כאן.", "before": "הטבלה שלהלן מציגה את שכר הדירה.", "heading": "3. שכר הדירה"}
    if where == "after":
        kw["after"] = stated + "."
    elif where == "before":
        kw["before"] = stated + "."
    elif where == "heading":
        kw["heading"] = "3. שכר הדירה לחודש"
    elif where == "caption":
        kw["caption"] = "שכר הדירה לחודש בנכס"
    else:
        kw["notes"] = (f"הערה: {stated}.",)
    a, r = _scoped(RENT_UNIT, _tables(_table(rows=RENT_ROWS, **{"caption": "שכר הדירה בנכס"} | kw)), _judge(lambda t: "supported"))
    assert r.qualifier_edits == {} and not any(p.check in ("meaning", "vat") for p in r.problems)
    assert r.removed_units() == set() and r.apply(a).answer_markdown == RENT_UNIT


def test_a_period_the_context_does_not_state_is_still_edited_out_as_before():
    a, r = _scoped(RENT_UNIT, _rent_ws(after="הערה כללית שאינה אומרת דבר על תקופה."), _judge(lambda t: "supported"))
    assert [p.check for p in r.problems] == ["meaning"] and r.removed_units() == set()
    assert r.edit(a).doc.text.startswith("שכר הדירה למשרדים הוא 5,000 ₪ [S1].")


def test_a_period_the_context_states_as_another_one_keeps_the_removal_path():
    a, r = _scoped(RENT_UNIT, _rent_ws(after="כל הסכומים בטבלה הם לשנה."), _judge(lambda t: "supported"))
    assert r.removed_units() == {0} and r.counts()["edited"] == 0


def test_a_period_the_number_own_row_states_as_another_one_is_not_waived_by_the_context_mentioning_the_claimed_one():
    """The row says per year; a trailing paragraph mentions a monthly figure elsewhere. The answer's per-month claim for the
    5,000 is contradicted by the number's own evidence, so the context must not waive it (it keeps the removal path)."""
    rows = "סוג | שכר דירה\nמשרדים | 5,000 ₪ לשנה"
    a, r = _scoped(RENT_UNIT, _tables(_table(rows=rows, caption="שכר הדירה בנכס",
                                             after="שכר הדירה החודשי של יחידות אחרות נמוך יותר ומפורט בנספח ג.")),
                   _judge(lambda t: "supported"))
    assert r.removed_units() == {0} and r.qualifier_edits == {}
    # the intended case stays waived: the row is silent on the period and the context states it
    a, r = _scoped(RENT_UNIT, _rent_ws(after="כל הסכומים בטבלה הם לחודש."), _judge(lambda t: "supported"))
    assert r.removed_units() == set() and r.qualifier_edits == {} and r.apply(a).answer_markdown == RENT_UNIT


def test_a_basis_the_sources_context_states_is_never_cut():
    unit = "דמי השכירות הם 96 ₪ למ\"ר ברוטו [S1]."
    a, r = _scoped(unit, _tables(_table(rows="סוג | דמי שכירות\nמשרדים | 96 ₪ למ\"ר", caption="דמי שכירות", before=None,
                                       after="השטחים בטבלה הם ברוטו.", heading="3. דמי השכירות")), _judge(lambda t: "supported"))
    assert r.qualifier_edits == {} and r.removed_units() == set() and r.apply(a).answer_markdown == unit


# --- where the server writes a period or basis note, and when it never does (round 18 U1, KTD4, R10, AE9) --------------------------
#
# The note "(…, כפי שנכתב במקור)" goes after the number's unit phrase, at the occurrence the problem names, and never after a
# duration (a number of months or years) or a count. Synthetic text only.

def _note_edit(markdown: str, number: str, annotation: str, at: int | None) -> str:
    units = split_units(markdown)
    problem = verify.Problem(units[0], "x", kind="missing_qualifier", number=number, annotation=annotation, at=at)
    return verify.VerifyReport(units, problems=[problem]).edit(_answer(markdown)).doc.text


NOTE = " (לחודש, כפי שנכתב במקור)"


@pytest.mark.parametrize("markdown, number, nth, expected", [
    # the second of two equal numbers: the note lands there, not after the first
    ("הנכס הראשון 50 ₪ למ\"ר והשני 50 ₪ למ\"ר [S1].", "50", 1, "הנכס הראשון 50 ₪ למ\"ר והשני 50 ₪ למ\"ר" + NOTE + " [S1]."),
    ("הנכס הראשון 50 ₪ למ\"ר והשני 50 ₪ למ\"ר [S1].", "50", 0, "הנכס הראשון 50 ₪ למ\"ר" + NOTE + " והשני 50 ₪ למ\"ר [S1]."),
    # after the whole unit phrase: currency, per-area words, shekel words and area units
    ("שווי הנכס 5,000 שקלים [S1].", "5,000", 0, "שווי הנכס 5,000 שקלים" + NOTE + " [S1]."),
    ("דמי השכירות 7,000 ש\"ח למטר [S1].", "7,000", 0, "דמי השכירות 7,000 ש\"ח למטר" + NOTE + " [S1]."),
    ("דמי החכירה 90 ₪ לדונם [S1].", "90", 0, "דמי החכירה 90 ₪ לדונם" + NOTE + " [S1]."),
    ("דמי השכירות 12 ₪ למ\"ר [S1].", "12", 0, "דמי השכירות 12 ₪ למ\"ר" + NOTE + " [S1]."),
    ("דמי השכירות 12 ו-30 ₪ [S1].", "12", 0, "דמי השכירות 12" + NOTE + " ו-30 ₪ [S1]."),  # no unit phrase: no space is taken
])
def test_a_period_note_goes_after_the_unit_phrase_at_the_occurrence_the_problem_names(markdown, number, nth, expected):
    (u,) = split_units(markdown)
    at = [m.start() for m in re.finditer(re.escape(number), u.text)][nth]
    assert _note_edit(markdown, number, "לחודש", at) == expected


@pytest.mark.parametrize("markdown, number", [
    ("תקופת השכירות היא 36 חודשים [S1].", "36"),
    ("תקופת השכירות היא 36 חודשי שכירות [S1].", "36"),
    ("תקופת הפטור היא 2 שנים [S1].", "2"),
    ("בבניין 12 דירות [S1].", "12"),
])
def test_a_period_note_is_never_written_after_a_duration_or_a_count(markdown, number):
    (u,) = split_units(markdown)
    assert _note_edit(markdown, number, "לחודש", u.text.index(number)) == markdown


def test_a_duration_in_the_unit_does_not_stop_the_note_of_the_amount_beside_it():
    markdown = "תקופת השכירות 36 חודשים ודמי השכירות 5,000 ₪ [S1]."
    (u,) = split_units(markdown)
    assert _note_edit(markdown, "5,000", "לחודש", u.text.index("5,000")) == (
        "תקופת השכירות 36 חודשים ודמי השכירות 5,000 ₪" + NOTE + " [S1].")


def test_the_note_lands_after_the_amount_not_after_an_identifier_written_with_the_same_digits():
    a, r = _report("בהיתר מס' 120 נקבע שטח של 120 מ\"ר [S1].", _ws("שטח 120 מ\"ר פלדלת."))
    assert [p.kind for p in r.problems] == ["missing_qualifier"]
    first = r.edit(a).doc.text.split("\n")[0]
    assert first == "בהיתר מס' 120 נקבע שטח של 120 מ\"ר (מ״ר פלדלת, כפי שנכתב במקור) [S1]."


def test_each_of_two_equal_amounts_is_noted_where_it_stands():
    a, r = _report("הנכס הראשון 50 ₪ למ\"ר והשני 50 ₪ למ\"ר [S1].", _ws("דמי הניהול 50 ₪ למ\"ר לחודש."))
    assert r.edit(a).doc.text.split("\n")[0] == (
        "הנכס הראשון 50 ₪ למ\"ר (לחודש, כפי שנכתב במקור) והשני 50 ₪ למ\"ר (לחודש, כפי שנכתב במקור) [S1].")


@pytest.mark.parametrize("source, claim", [
    ("שכר דירה חודש י 5,000 ₪.", "שכר הדירה הוא 5,000 ₪ לחודש [S1]."),      # a split monthly word still backs the claim
    ("דמי שכירות חודשיים 5,000 ₪.", "דמי השכירות הם 5,000 ₪ לחודש [S1]."),   # a plural adjective after a plural noun
    ("שכר דירה 5,000 ₪ ל חודש.", "שכר הדירה הוא 5,000 ₪ לחודש [S1]."),
])
def test_a_period_the_source_writes_in_a_split_or_plural_form_is_not_removed_from_the_answer(source, claim):
    a, r = _report(claim, _ws(source))
    assert r.problems == [] and r.removed_units() == set() and r.qualifier_edits == {}
    assert r.edit(a).doc.text.startswith(claim)


def test_a_split_monthly_word_gives_the_missing_period_note_after_the_amount():
    a, r = _report("שכר הדירה הוא 5,000 ₪ [S1].", _ws("שכר דירה חודש י 5,000 ₪."))
    assert r.edit(a).doc.text.split("\n")[0] == "שכר הדירה הוא 5,000 ₪ (לחודש, כפי שנכתב במקור) [S1]."


@pytest.mark.parametrize("source, claim", [
    ("תקופת השכירות 36 חודשי ם, ודמי השכירות 4,700 ₪ לחודש.", "תקופת השכירות היא 36 חודשים [S1]."),
    ("תקופת השכירות 36 חודשי שכירות, ודמי השכירות 4,700 ₪ לחודש.", "תקופת השכירות היא 36 חודשי שכירות [S1]."),
])
def test_a_months_count_is_neither_removed_nor_given_a_monthly_note(source, claim):
    a, r = _report(claim, _ws(source))
    assert r.problems == [] and r.removed_units() == set()
    assert r.edit(a).doc.text.split("\n")[0] == claim


def test_the_lease_term_and_the_rent_in_one_sentence_keep_their_own_meaning():
    source = "תקופת השכירות 36 חודשים, שכר הדירה 50 ₪ למ\"ר לחודש."
    a, r = _report("תקופת השכירות 36 חודשים ושכר הדירה 50 ₪ למ\"ר לחודש [S1].", _ws(source))
    assert r.problems == [] and r.removed_units() == set()
    a, r = _report("תקופת השכירות 36 חודשים ושכר הדירה 50 ₪ למ\"ר [S1].", _ws(source))
    assert r.edit(a).doc.text.split("\n")[0] == (
        "תקופת השכירות 36 חודשים ושכר הדירה 50 ₪ למ\"ר (לחודש, כפי שנכתב במקור) [S1].")


def test_a_period_the_source_does_not_state_is_edited_out_beside_a_number_that_ends_with_a_comma():
    """The number token "5,000," keeps no comma where its year and identifier tests and the edit read it, so the qualifier
    edit still finds the number its blocking problem names."""
    a, r = _report("שכר הדירה לחודש הוא 5,000, בלבד [V1].", _silent_period())
    assert r.removed_units() == set() and [q.kind for q in r.qualifier_edits[0]] == ["period"]
    assert r.edit(a).doc.text.startswith("שכר הדירה הוא 5,000, בלבד [V1].")
