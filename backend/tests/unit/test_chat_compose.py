"""One composer for every edit after the model's answer (round 8 U4, KTD6): the server's words are recorded as spans of
the final Markdown — offsets and a fixed kind, never text — and the registry indexes the FINAL text exactly, whatever was
cut, collapsed or appended after the span was written. The judge and the model are scripted; synthetic text only."""

from __future__ import annotations

import json
import re
import uuid

from app.chat import coverage, verify
from app.chat.compose import MARKER, SERVER_KINDS, Doc, compose
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import TurnRequirements, verify_answer
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider
from tests.unit.test_coverage import _by_words
from tests.unit.test_coverage import _judge as _scored_judge

SOURCE = ("סיכום: השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש. דמי הניהול הם 12 ₪ למ\"ר. "
          "הנכס פנוי.")


def _ws(*texts: str) -> Workspace:
    ws = Workspace(ctx=None)
    for t in texts or (SOURCE,):
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                      location="סעיף \"סיכום\"", kind="context", text=t)
    return ws


def _answer(markdown: str, status: str = "answered") -> FinalAnswer:
    return FinalAnswer(status=status, answer_markdown=markdown, items=[], claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def _judge(respond) -> ScriptedProvider:
    """``respond(text) -> verdict dict fields`` per unit (the index is added)."""
    p = ScriptedProvider()

    def answer(instructions: str, input: str) -> dict:
        return {"verdicts": [{"index": int(i), "reason": "בדיקה"} | respond(t)
                             for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)]}

    p.on(Purpose.VERIFY, answer, repeat=True)
    return p


def _texts(markdown: str, spans) -> list[tuple[str, str]]:
    return [(s.kind, markdown[s.start:s.end]) for s in spans]


# --- the span-tracking text -------------------------------------------------------------------------------------------

def test_a_doc_keeps_its_spans_exact_through_splices_deletions_and_whitespace_clean_up():
    d = Doc("  א ב ג\n\n\n\nד ה  ")
    d.splice(4, 5, "[X]", "citation")  # ב -> the server's [X]
    d.delete([(0, 2)])
    d.append("\n\nסוף", "limit_sentence")
    d.tidy_blank_lines()
    assert d.text == "א [X] ג\n\nד ה  \n\nסוף"
    assert _texts(d.text, d.spans()) == [("citation", "[X]"), ("limit_sentence", "\n\nסוף")]
    assert d.masked() == "א  ג\n\nד ה  "


def test_the_ordinal_of_a_marker_follows_its_place_after_an_earlier_one_is_removed():
    d = Doc("א ב ג")
    d.splice(0, 1, MARKER.format(n=7), "marker", ("c1",))
    d.splice(len(d.text) - 1, len(d.text), MARKER.format(n=9), "marker", ("c2",))
    first = d.spans()[0]
    d.delete([(first.start, first.end)])  # the first marker goes (its unit was removed)
    assert d.renumber("marker", MARKER) == [(1, ("c2",))]
    assert d.text == " ב " + MARKER.format(n=1)
    assert _texts(d.text, d.spans()) == [("marker", MARKER.format(n=1))]


def test_every_server_kind_is_a_fixed_word_and_a_span_of_an_unknown_kind_is_refused():
    assert len(set(SERVER_KINDS)) == len(SERVER_KINDS) == 15
    try:
        Doc("x").splice(0, 0, "y", "free text")
    except AssertionError:
        return
    raise AssertionError("a kind outside SERVER_KINDS was accepted")


# --- the report's own edits: markers, partial marks, qualifiers, citations, notes, the replacement ------------------

def _kinds_report():
    """One answer with a span cut, a citation the server inserts, a partly supported sentence, a removed sentence and
    a request that does not match: every kind ``VerifyReport.edit`` writes but the replacement."""
    a = _answer("הנכס פנוי, והוא מושכר [S1].\nהשווי למ\"ר הוא 9,500 ₪.\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\n"
                "דמי הניהול הם 14 ₪ למ\"ר [S1].")

    def respond(text: str) -> dict:
        if "מושכר" in text:
            return {"verdict": "partial", "defect": "part", "claims": [{"span": "הנכס פנוי", "verdict": "supported"},
                                                                       {"span": "והוא מושכר", "verdict": "unsupported"}]}
        if text.startswith("השווי"):
            return {"verdict": "supported", "supported_by": ["S1"]}
        if "דמי השכירות" in text:
            return {"verdict": "partial", "defect": "none"}
        return {"verdict": "unsupported", "failure": "absent_from_source"}

    r = verify_answer(_judge(respond), a, _ws(), "?", [], mismatch="שווי במקום שכירות")
    return a, r


def test_the_registry_of_a_reports_edits_indexes_the_final_text_exactly_for_every_kind_it_writes():
    a, r = _kinds_report()
    edited = r.edit(a)
    text, spans = edited.doc.text, edited.doc.spans()
    assert [k for k, _ in _texts(text, spans)] == ["marker", "citation", "partial_mark", "removal_note", "request_note"]
    assert dict(_texts(text, spans)) == {
        "marker": MARKER.format(n=1), "citation": " [S1]", "partial_mark": " *(אומת חלקית)*",
        "removal_note": "\n\n> " + verify.REMOVAL_SENTENCES["absent_from_source"],
        "request_note": "\n\n> **שימו לב:** התשובה אינה מציגה את הנתון שהתבקש: שווי במקום שכירות."}
    # nothing but those spans is the server's: with them masked, what is left is the model's text, minus the cuts
    assert edited.doc.masked() == ("הנכס פנוי,  [S1].\nהשווי למ\"ר הוא 9,500 ₪.\n"
                                   "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].")
    assert text.startswith("הנכס פנוי, " + MARKER.format(n=1))


def test_a_qualifier_written_from_the_source_is_a_span_and_the_model_text_around_it_is_untouched():
    qualified = "בפרק התחשיב: השווי למ\"ר אקוו' לנכס ברחוב הצאלון 7 נקבע ל-14,250 ₪, ללא מע\"מ."
    a = _answer("השווי למ\"ר בצאלון 7 הוא 14,250 ₪ [S1].")
    r = verify_answer(_judge(lambda t: {"verdict": "supported"}), a, _ws(qualified), "?", [])
    edited = r.edit(a)
    assert edited.doc.text == "השווי למ\"ר בצאלון 7 הוא 14,250 ₪ (מ״ר אקוו׳, כפי שנכתב במקור) [S1]."
    assert _texts(edited.doc.text, edited.doc.spans()) == [("qualifier", " (מ״ר אקוו׳, כפי שנכתב במקור)")]
    assert edited.doc.masked() == a.answer_markdown


def test_an_answer_nothing_of_which_is_supported_is_replaced_and_the_replacement_is_one_span():
    a = _answer("הנכס מושכר [S1].")
    r = verify_answer(_judge(lambda t: {"verdict": "unsupported", "failure": "absent_from_source"}), a, _ws(), "?", [])
    edited = r.edit(a)
    assert edited.doc.text == "לא הצלחתי לבסס תשובה על המקורות." and edited.status == "partial"
    assert _texts(edited.doc.text, edited.doc.spans()) == [("replacement", edited.doc.text)]
    assert edited.doc.masked() == ""


def test_an_answer_with_nothing_to_edit_has_no_server_span():
    a = _answer("הנכס פנוי [S1].")
    r = verify_answer(_judge(lambda t: {"verdict": "supported"}), a, _ws(), "?", [])
    edited = r.edit(a)
    assert edited.doc.text == a.answer_markdown and edited.doc.spans() == [] and r.apply(a).answer_markdown == a.answer_markdown


# --- compose: the gap paragraph, the limit sentence and the coverage note join the same registry -----------------------

def test_compose_registers_the_gap_paragraph_the_limit_sentence_and_the_coverage_note_with_exact_offsets(monkeypatch):
    note = "> **כיסוי:** לא כל המסמכים המתאימים נבדקו."
    monkeypatch.setattr(coverage, "build", lambda ws, answer, question: (
        {"note": note}, answer.model_copy(update={"answer_markdown": answer.answer_markdown + "\n\n" + note})))
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\nהנכס מושכר [S1].")
    p = _scored_judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": ["H1"]}}),
                      rule=lambda t: "unsupported" if "מושכר" in t else "supported")
    r = verify_answer(p, a, ws, "מה השווי למ\"ר ומה דמי הניהול?", [], requirements=turn)
    limit = "החיפוש והקריאה נעצרו במספר הצעדים המרבי."
    out = compose(ws, a, r, turn, question="?", limit_sentences=[limit])
    text = out.answer.answer_markdown
    got = _texts(text, out.spans)
    assert [k for k, _ in got] == ["removal_note", "gap_paragraph", "limit_sentence", "coverage_note"], got
    by_kind = dict(got)
    assert by_kind["gap_paragraph"].strip() == "**דמי הניהול** לא נמצא בחיפוש במסמכים שנבדקו."
    assert by_kind["limit_sentence"].strip() == limit and by_kind["coverage_note"].strip() == note
    assert by_kind["removal_note"].strip() == "> " + verify.REMOVAL_SENTENCES["absent_from_source"]
    # every span lies inside the text and none overlaps another
    ends = [0] + [x for s in out.spans for x in (s.start, s.end)]
    assert ends == sorted(ends) and out.spans[-1].end == len(text)
    # the model's text is all that is left outside the spans
    model = "".join(text[a_:b_] for a_, b_ in zip([0] + [s.end for s in out.spans], [s.start for s in out.spans] + [len(text)],
                                                    strict=True))
    assert model == "השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1]."
    assert out.answer.status == "partial" and out.outcomes and out.ledger == {"note": note}


def test_a_membership_note_is_its_own_kind(monkeypatch):
    note = "> **כיסוי:** רשימת 3 מסמכים נבדקה."
    monkeypatch.setattr(coverage, "build", lambda ws, answer, question: (
        {"membership": True}, answer.model_copy(update={"answer_markdown": answer.answer_markdown + "\n\n" + note})))
    a = _answer("הנכס פנוי [S1].")
    r = verify_answer(_judge(lambda t: {"verdict": "supported"}), a, _ws(), "?", [])
    out = compose(_ws(), a, r, TurnRequirements(), question="?")
    assert _texts(out.answer.answer_markdown, out.spans) == [("membership_note", "\n\n" + note)]


def test_a_clarification_keeps_its_question_and_gets_no_ledger_or_span(monkeypatch):
    monkeypatch.setattr(coverage, "build", lambda *a: (_ for _ in ()).throw(AssertionError("no ledger for a question")))
    a = _answer("מהו שיעור ההיוון שברצונך להשתמש בו?", "clarification")
    r = verify_answer(_judge(lambda t: {"verdict": "not_factual"}), a, _ws(), "?", [])
    out = compose(_ws(), a, r, TurnRequirements(), question="?", limit_sentences=["משפט מגבלה"])
    assert out.answer.answer_markdown == a.answer_markdown and out.answer.status == "clarification"
    assert out.spans == [] and out.ledger == {}


def test_the_ledger_marks_a_cited_document_verified_only_when_the_engine_asks(monkeypatch):
    import contextlib

    from app.platform import search

    monkeypatch.setattr(coverage, "tenant_tx", lambda ctx: contextlib.nullcontext(None))
    monkeypatch.setattr(search, "documents_named", lambda conn, q: {})
    monkeypatch.setattr(search, "documents_sharing_title_words", lambda conn, q: {})
    ws = _ws()
    doc = str(ws.sources["S1"].document_id)
    a = _answer("הנכס פנוי [S1].")
    ledger, built = coverage.build(ws, a, "?")  # the real ledger: it no longer touches the workspace
    assert doc not in ws.activity and ledger["cited"][0]["document_id"] == doc and built.answer_markdown == a.answer_markdown
    coverage.touch_verified(ws, built.answer_markdown)  # the engine's step, once, for the final text's documents
    assert ws.activity[doc]["level"] == "verified"


# --- round 8 U6: where each character came from, and the answer without the server's words ---------------------------

def test_a_doc_knows_the_draft_offset_of_every_character_the_model_wrote_and_marks_the_servers_with_minus_one():
    d = Doc("  א ב ג\n\n\n\nד ה  ")
    assert d.origins() == list(range(len("  א ב ג\n\n\n\nד ה  ")))
    d.splice(4, 5, "[X]", "citation")  # ב -> the server's [X]
    d.delete([(0, 2)])
    d.append("\n\nסוף", "limit_sentence")
    d.tidy_blank_lines()
    assert d.text == "א [X] ג\n\nד ה  \n\nסוף"
    origins = d.origins()
    assert len(origins) == len(d.text)
    assert [d.text[i] for i, o in enumerate(origins) if o < 0] == list("[X]") + list("\n\nסוף")
    # a character of the model's own text keeps the place it had in what the model wrote
    original = "  א ב ג\n\n\n\nד ה  "
    assert all(original[o] == d.text[i] for i, o in enumerate(origins) if o >= 0)


def test_compose_hands_the_origin_of_every_character_of_the_final_text(monkeypatch):
    note = "> **כיסוי:** לא כל המסמכים המתאימים נבדקו."
    monkeypatch.setattr(coverage, "build", lambda ws, answer, question: (
        {"note": note}, answer.model_copy(update={"answer_markdown": answer.answer_markdown + "\n\n" + note})))
    a, r = _kinds_report()
    out = compose(_ws(), a, r, TurnRequirements(), question="?")
    text = out.answer.answer_markdown
    assert len(out.origin) == len(text)
    servers = {i for s in out.spans for i in range(s.start, s.end)}
    assert {i for i, o in enumerate(out.origin) if o < 0} == servers
    assert all(a.answer_markdown[o] == text[i] for i, o in enumerate(out.origin) if o >= 0)


def test_the_answer_without_the_servers_spans_for_a_later_turn(monkeypatch):
    from app.chat.compose import ServerSpan, strip_server_spans

    monkeypatch.setattr(coverage, "build", lambda ws, answer, question: ({}, answer))
    a, r = _kinds_report()
    out = compose(_ws(), a, r, TurnRequirements(), question="?")
    text = out.answer.answer_markdown
    stripped = strip_server_spans(text, [s.as_dict() for s in out.spans])  # as the saved message holds them
    assert stripped == strip_server_spans(text, out.spans)
    assert verify.REMOVAL_SENTENCES["absent_from_source"] not in stripped and "חלק שלא אומת" not in stripped and "אומת חלקית" not in stripped
    assert "שימו לב" not in stripped
    assert "הנכס פנוי," in stripped and "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1]." in stripped
    assert "  " not in stripped and "\n\n\n" not in stripped
    # an older message has no registry, a registry that does not index its text is not trusted: the text as it is
    assert strip_server_spans(text, None) == text and strip_server_spans(text, []) == text
    assert strip_server_spans("שלום", [{"start": 2, "end": 99, "kind": "marker"}]) == "שלום"
    assert strip_server_spans("א ב ג", [ServerSpan(1, 3, "marker")]) == "א ג"


# --- round 8 R29: a clarification keeps its question and its status through every edit ----------------------------------

QUESTION = "מהו שיעור ההיוון שברצונך להשתמש בו?"


def test_a_clarification_whose_claims_all_fail_keeps_its_question_and_its_status_through_the_edit():
    a = _answer(f"הנכס מושכר [S1].\n{QUESTION}", "clarification")
    a = a.model_copy(update={"clarification_question": QUESTION})
    r = verify_answer(_judge(lambda t: {"verdict": "unsupported", "failure": "absent_from_source"}
                             if "מושכר" in t else {"verdict": "not_factual"}), a, _ws(), "?", [])
    edited = r.edit(a)
    assert edited.status == "clarification"
    assert edited.doc.text.startswith(QUESTION) and "מושכר" not in edited.doc.text
    assert "לא הצלחתי" not in edited.doc.text and [s.kind for s in edited.doc.spans()] == ["removal_note"]


def test_the_unit_that_holds_the_question_is_never_removed_even_when_a_check_fails_it():
    question = "האם להשתמש בשיעור 40% [S1] כשיעור ההיוון?"
    a = _answer(f"הנכס מושכר [S1].\n{question}", "clarification").model_copy(update={"clarification_question": question})
    r = verify_answer(_judge(lambda t: {"verdict": "unsupported", "failure": "absent_from_source"}), a, _ws(), "?", [])
    assert r.protected == {1} and r.removed_units() == {0}
    assert "האם להשתמש בשיעור 40%" in r.edit(a).doc.text
    # a draft that is not a clarification has no protected unit
    plain = verify_answer(_judge(lambda t: {"verdict": "supported"}), _answer("הנכס פנוי [S1]."), _ws(), "?", [])
    assert plain.protected == set()


# --- round 14 U7 (R14, KTD9, AE10): a partial mark carries its ordinal and the unit's partly supported claims -----------

RENT = "דמי השכירות הם 55 ₪ למ\"ר לחודש"


def _partial_report(*, two_claims: bool = False):
    """The rent sentence is partly supported for an attribute the source does not state (``units``); the first and the
    last sentence are supported. With ``two_claims`` the rent sentence holds two partly supported claims."""
    a = _answer(f"הנכס פנוי [S1].\n{RENT} [S1].\nהשווי למ\"ר הוא 9,500 ₪ [S1].\nדמי הניהול הם 12 ₪ למ\"ר לחודש [S1].")

    def respond(text: str) -> dict:
        if "השכירות" in text and two_claims:
            return {"verdict": "partial", "defect": "units",
                    "claims": [{"span": "דמי השכירות הם 55 ₪", "verdict": "partial"},
                               {"span": "למ\"ר לחודש", "verdict": "partial"}]}
        if "לחודש" in text:
            return {"verdict": "partial", "defect": "units"}
        return {"verdict": "supported"}

    return a, verify_answer(_judge(respond), a, _ws(), "?", [])


def _claim_ids(report, needle: str) -> list[str]:
    """The ids of every claim of the unit that has a claim whose text holds ``needle``."""
    return [r.id for recs in report.claims.values() if any(needle in x.text for x in recs) for r in recs]


def _no_ledger(monkeypatch) -> None:
    monkeypatch.setattr(coverage, "build", lambda ws, answer, question: ({}, answer))


def test_a_partial_mark_span_carries_its_ordinal_and_the_failing_claim_ids_in_the_texts_order():
    a, r = _partial_report()
    edited = r.edit(a)
    marks = [s for s in edited.doc.spans() if s.kind == "partial_mark"]
    assert [m.ordinal for m in marks] == [1, 2], marks  # two units are marked: the rent and the management fee
    assert marks[0].claim_ids == tuple(_claim_ids(r, "השכירות")) and len(marks[0].claim_ids) == 1
    assert marks[1].claim_ids == tuple(_claim_ids(r, "הניהול"))
    # the registry as saved: offsets and a kind, and a mark's ordinal and claims; no other span carries them
    saved = [s.as_dict() for s in edited.doc.spans()]
    assert all(set(s) == ({"start", "end", "kind", "ordinal", "claim_ids"} if s["kind"] == "partial_mark"
                          else {"start", "end", "kind"}) for s in saved), saved
    assert [s["ordinal"] for s in saved if s["kind"] == "partial_mark"] == [1, 2]


def test_a_table_row_has_one_mark_at_each_cell_end_whatever_the_number_of_problems_that_name_the_cell(monkeypatch):
    _no_ledger(monkeypatch)
    md = "| נתון | ערך | הערה | פירוט |\n|---|---|---|---|\n| השווי למ\"ר | 9,500 ₪ [S1] | הערה א | פירוט ב |"
    a = _answer(md)
    r = verify_answer(_judge(lambda t: {"verdict": "supported", "supported_by": ["S1"]}), a, _ws(), "?", [])
    row = next(u for u in r.units if "9,500" in u.text)
    note, more = (row.start + row.raw.index(x) + len(x) for x in ("הערה א", "פירוט ב"))
    for claim, end in (("C1", note), ("C2", note), ("C3", more), ("C3", more)):  # two claims in one cell, one claim named twice
        r.problems.append(verify.Problem(row, "חלקי", "partial", defect="none", check="final", claim_id=claim, cell_end=end))
    edited = r.edit(a)
    marks = [s for s in edited.doc.spans() if s.kind == "partial_mark"]
    assert [m.claim_ids for m in marks] == [("C1", "C2"), ("C3",)]
    line = next(ln for ln in edited.doc.text.splitlines() if "9,500" in ln)
    cells = [c.strip() for c in line.strip().strip("|").split("|")]
    assert len(cells) == 4 and cells[2].count("אומת חלקית") == cells[3].count("אומת חלקית") == 1 and "אומת חלקית" not in cells[1]


def test_a_unit_with_two_partly_supported_claims_has_one_mark_carrying_both_claim_ids():
    a, r = _partial_report(two_claims=True)
    edited = r.edit(a)
    marks = [s for s in edited.doc.spans() if s.kind == "partial_mark"]
    rent = [m for m in marks if len(m.claim_ids) > 1]
    assert len(marks) == 2 and len(rent) == 1, marks
    assert set(rent[0].claim_ids) == set(_claim_ids(r, "השכירות")) and len(rent[0].claim_ids) == 2


def test_compose_records_the_ordinals_of_the_marks_that_reached_the_final_text_on_the_report_and_its_rows(monkeypatch):
    _no_ledger(monkeypatch)
    a, r = _partial_report()
    out = compose(_ws(), a, r, TurnRequirements(), question="?")
    marks = [s for s in out.spans if s.kind == "partial_mark"]
    assert [(m.ordinal, m.claim_ids) for m in marks] == r.partial_marks
    rows = r.counts()["partials"]
    assert [row["ordinal"] for row in rows] == [1, 2]
    assert [row["claim_id"] for row in rows] == [m.claim_ids[0] for m in marks]
    # a row is a failure kind, a check, the claims and the server's fixed sentence: never the sentence's own words
    assert rows[0]["failure_kind"] == "wrong_unit" and rows[0]["check"] == "judge"
    assert rows[0]["text"] == verify.PARTIAL_SENTENCES["wrong_unit"]
    assert RENT not in json.dumps(rows, ensure_ascii=False)


def test_every_logged_qualified_claim_of_a_mark_pairs_with_one_diagnostics_entry_by_claim_id(monkeypatch):
    _no_ledger(monkeypatch)
    a, r = _partial_report(two_claims=True)
    compose(_ws(), a, r, TurnRequirements(), question="?")
    entries = {e.claim_id: e for e in r.logged() if e.action == "qualified"}
    for row in r.counts()["partials"]:
        assert row["claim_ids"] and all(cid in entries for cid in row["claim_ids"]), (row, list(entries))
