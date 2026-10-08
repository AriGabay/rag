"""Completeness against requirements the judge derives from the request (U9, KTD7, R18–R22).

The first judge call of a turn derives what the request requires (the answer's declared parts are hints only),
the list is frozen with stable ids, and every later judge call scores it by id. A requirement the verified answer
does not give is stated with a reason computed from what the turn found and did — never "not found in the search"
when its values or calculation were found. The judge here is scripted: these tests prove what the server does with
its requirements and scores, not the model's judgement. Synthetic text and values only."""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal

from app.chat import calc, coverage, verify
from app.chat.engine import FinalAnswer, Part, Requested
from app.chat.tools import Workspace
from app.chat.verify import TurnRequirements, verify_answer
from app.providers.llm import Purpose
from tests.support.scripted_provider import ScriptedProvider

SOURCE = "סיכום: השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש. הנכס פנוי."
NOT_FOUND = "לא נמצא בחיפוש"


def _ws(*texts: str) -> Workspace:
    ws = Workspace(ctx=None)
    for t in texts or (SOURCE,):
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                      location="סעיף \"סיכום\"", kind="context", text=t)
    return ws


def _value(ws: Workspace, written: str, label: str, kind: str = "other", *, subject: str = "נכס הדגמה",
           document=None, asserted: bool = False) -> str:
    vid = f"V{len(ws.values) + 1}"
    ws.values[vid] = calc.Value(vid, Decimal(written.replace(",", "")), written, "S1", document or uuid.uuid4(),
                                uuid.uuid4(), None, "דוח בדיקה", "סעיף \"סיכום\"", label, kind, "ILS", "none",
                                "unknown", "", subject, "other",
                                {"unit": "model_asserted" if asserted else "source"}, {"quote": f"{label} {written}"},
                                f"{label} {written}")
    return vid


def _answer(markdown: str, *, requested: list | None = None, parts: list | None = None) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=requested or [], parts=parts or [])


def _req(text: str = "", status: str = "full", units=(), related=(), id: str = "", calculation: bool = False) -> dict:
    return {"id": id, "text": text, "calculation": calculation, "status": status, "units": list(units),
            "related": list(related), "reason": "בדיקה"}


def _judge(score, rule=lambda text: "supported", seen: list | None = None) -> ScriptedProvider:
    """A judge answering each unit by ``rule(text)`` and the requirements by ``score(frozen, units, statements)``:
    ``frozen`` is None on the call that derives them, else [(id, text)]; ``units`` and ``statements`` are
    {index: text} of the call."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        units = {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}
        statements = {int(i): t for i, t in re.findall(r'<statement index="(\d+)">\n(.*?)\n</statement>', input,
                                                         re.S)}
        frozen = None if "<derive_requirements>" in input else re.findall(
            r'<requirement id="(Q\d+)"[^>]*>\n(.*?)\n</requirement>', input, re.S)
        out = {"verdicts": [{"index": i, "verdict": rule(t), "reason": "בדיקה"} for i, t in units.items()]}
        if "<derive_requirements>" in input or "<requirements>" in input:
            out["requirements"] = score(frozen, units, statements)
        return out

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _by_words(asks: dict[str, dict], missing_status: str = "missing"):
    """Score each ask: ``full`` with the units that name it, else ``missing_status`` (with ``related`` from
    ``asks[ask]``, and as its units the units or statements that name it as not found)."""
    def score(frozen, units, statements):
        out = []
        names = [t for _, t in frozen] if frozen is not None else list(asks)
        for n, ask in enumerate(names):
            hits = [i for i, t in units.items() if ask in t and "לא נמצא" not in t]
            said = [i for i, t in (units | statements).items() if ask in t and i not in hits]
            extra = asks.get(ask) or {}
            out.append(_req(ask, "full" if hits else missing_status, hits or said, extra.get("related", ()),
                            frozen[n][0] if frozen is not None else "", extra.get("calculation", False)))
        return out
    return score


def _finish(ws: Workspace, answer: FinalAnswer, report, turn: TurnRequirements) -> tuple[FinalAnswer, list[dict]]:
    """What the engine does with a verified answer: removals, the server's absence sentences, the requirements."""
    final = coverage.state_absence(ws, report.apply(answer), cited=False, withdrawn=report.withdrawn)
    final, outcomes = coverage.state_parts(ws, final, report, turn)
    report.completeness = coverage.completeness(outcomes)
    return final, outcomes


# --- requirements come from the judge, not from the answer's own parts (R18, KTD7) ------------------------------

def test_an_answer_that_declares_no_parts_still_gets_requirements_and_a_missing_one_is_stated():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    seen: list[str] = []
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": ["H1"]}}), seen=seen)
    r = verify_answer(p, a, ws, "מה השווי למ\"ר ומה דמי הניהול?", [], requirements=turn)
    assert "<derive_requirements>" in seen[0] and "<workspace>" in seen[0] and "H1" in seen[0]
    assert [(x["id"], x["text"]) for x in turn.items] == [("Q1", "השווי למ\"ר"), ("Q2", "דמי הניהול")]
    final, outcomes = _finish(ws, a, r, turn)
    assert final.status == "partial"
    assert final.answer_markdown.startswith("**דמי הניהול** לא נמצא בחיפוש במסמכים שנבדקו.")
    assert [(o["id"], o["status"], o["limitation"]) for o in outcomes] == [
        ("Q1", "full", None), ("Q2", "missing", "not_found")]


def test_the_declared_parts_are_hints_in_the_deriving_call_only():
    ws = _ws()
    turn = TurnRequirements()
    seen: list[str] = []
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].", parts=[Part(ask="השווי למ\"ר", answered=True, missing_kind="none")])
    p = _judge(_by_words({"השווי למ\"ר": {}}), seen=seen)
    verify_answer(p, a, ws, "?", [], requirements=turn)
    verify_answer(p, a, ws, "?", [], requirements=turn)
    assert "<hint>" in seen[0] and "<hint>" not in seen[1] and "<derive_requirements>" not in seen[1]
    assert '<requirement id="Q1"' in seen[1]


def test_requirements_derived_in_the_first_batch_are_scored_by_id_in_the_next_batch_and_on_a_rejudge(monkeypatch):
    monkeypatch.setattr(verify, "JUDGE_MAX_UNITS", 1)
    ws = _ws()
    turn = TurnRequirements()
    seen: list[str] = []
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי השכירות": {}}), seen=seen)
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    assert len(seen) == 2 and "<derive_requirements>" in seen[0]
    assert '<requirement id="Q1"' in seen[1] and '<requirement id="Q2"' in seen[1]
    # the second requirement is missing in the first batch and given in the second: merged by id
    assert [(o["id"], o["status"]) for o in r.requirement_outcomes()] == [("Q1", "full"), ("Q2", "full")]
    again = verify_answer(p, a, ws, "?", [], requirements=turn)
    assert "<derive_requirements>" not in seen[2] and [x["id"] for x in turn.items] == ["Q1", "Q2"]
    assert [(o["id"], o["status"]) for o in again.requirement_outcomes()] == [("Q1", "full"), ("Q2", "full")]


def test_requirements_are_derived_even_when_no_unit_reaches_the_judge():
    ws = _ws()
    turn = TurnRequirements()
    seen: list[str] = []
    a = _answer("השווי למ\"ר הוא 1,234 ₪ [S1].")  # fails the number check: nothing left to judge
    verify_answer(_judge(_by_words({"השווי למ\"ר": {}}), seen=seen), a, ws, "?", [], requirements=turn)
    assert len(seen) == 1 and "<derive_requirements>" in seen[0] and "<unit index" not in seen[0]
    assert [x["text"] for x in turn.items] == ["השווי למ\"ר"]


def test_without_a_turn_state_the_judge_is_not_asked_for_requirements():
    seen: list[str] = []
    p = _judge(lambda *a: [], seen=seen)
    verify_answer(p, _answer("השווי למ\"ר הוא 9,500 ₪ [S1]."), _ws(), "?", [])
    assert "<derive_requirements>" not in seen[0] and p.calls[0].schema is verify.JudgeOutput


def test_the_policy_and_schema_carry_the_four_statuses_and_the_related_ids():
    assert set(verify.REQUIREMENT_STATUSES) == {"full", "partial", "missing", "undeterminable"}
    fields = verify.JudgeRequirement.model_fields
    assert {"id", "text", "status", "units", "related", "calculation"} <= set(fields)
    assert "undeterminable" in verify.JUDGE_REQUIREMENTS_POLICY and "related" in verify.JUDGE_REQUIREMENTS_POLICY


def test_a_requirement_given_only_by_a_removed_claim_is_missing_and_stated():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי הניהול הם 9,500 ₪ [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": ["H1"]}}),
               rule=lambda t: "unsupported" if "ניהול" in t else "supported")
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    assert outcomes[1]["status"] == "missing" and not outcomes[1]["stated"]
    assert final.answer_markdown.startswith("**דמי הניהול** לא נמצא בחיפוש") and final.status == "partial"


def test_a_requirement_no_call_scored_is_never_taken_as_given():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")

    def score(frozen, units, statements):  # derives two requirements and scores only the first
        if frozen:
            return [_req(status="full", units=list(units), id=frozen[0][0])]
        return [_req("השווי למ\"ר", "full", list(units)), _req("דמי הניהול", "full", [])]

    verify_answer(_judge(score), a, ws, "?", [], requirements=turn)  # derived: the second given by no unit
    r = verify_answer(_judge(score), a, ws, "?", [], requirements=turn)  # re-judged: the second is not scored
    assert [(o["id"], o["status"]) for o in r.requirement_outcomes()] == [("Q1", "full"), ("Q2", "missing")]


# --- a missing requirement whose data exist is a repair problem (R20, AE6) ---------------------------------------

def test_a_missing_requirement_whose_values_were_found_is_a_repair_problem_not_a_removal():
    ws = _ws()
    v1 = _value(ws, "9,500", "השווי למ\"ר")
    v2 = _value(ws, "8,000", "סף ההשוואה")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "ההשוואה לסף": {"related": [v1, v2], "calculation": True}}))
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    (problem,) = [x for x in r.problems if x.kind == "requirement"]
    assert not r.ok and not r.removed_units() and not problem.removes_unit
    assert "ההשוואה לסף" in r.problems_text() and v1 in problem.reason and v2 in problem.reason
    assert r.apply(a).answer_markdown == a.answer_markdown  # nothing to remove: a repair round completes it


def test_a_requirement_no_search_covered_is_a_repair_problem_and_then_stated_as_not_searched():
    ws = _ws()
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    r = verify_answer(_judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {}})), a, ws, "?", [], requirements=turn)
    assert [x.kind for x in r.problems] == ["requirement"] and "לחפש" in r.problems_text()
    final, outcomes = _finish(ws, a, r, turn)
    assert NOT_FOUND not in final.answer_markdown and "לא נמצא" not in final.answer_markdown
    assert final.answer_markdown.startswith("**דמי הניהול** לא נבדק")
    assert outcomes[1]["limitation"] == "not_searched"


def test_a_failed_calculation_is_stated_as_not_completed_never_as_not_found():
    ws = _ws()
    v1 = _value(ws, "9,500", "השווי למ\"ר")
    turn = TurnRequirements()
    turn.record("calculate", json.dumps({"expression": "V1 / V2", "label": "היחס לסף"}),
                f"שגיאה: אי אפשר לחשב: חלוקה באפס (V1 / V2). {calc.MSG_FAILED}")
    turn.record("search", json.dumps({"query": "סף"}), "תוצאות")  # not a failure
    assert [(x["id"], x["kind"]) for x in turn.incidents] == [("F1", "calculation")]
    seen: list[str] = []
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "היחס לסף": {"related": [v1, "F1"], "calculation": True}}), seen=seen)
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    assert "F1" in seen[0] and "היחס לסף" in seen[0]
    final, outcomes = _finish(ws, a, r, turn)
    assert "לא נמצא" not in final.answer_markdown
    assert final.answer_markdown.startswith("**היחס לסף**: החישוב נכשל על הנתונים שנמצאו")
    assert outcomes[1]["limitation"] == "calculation_incomplete"
    assert r.counts()["completeness"]["missing"] == [{
        "id": "Q2", "text": "היחס לסף", "status": "missing", "reason": "calculation_incomplete",
        "reason_text": coverage.LIMITATIONS["calculation_incomplete"]}]


def test_found_inputs_of_a_calculation_never_computed_say_the_calculation_was_not_completed():
    ws = _ws()
    v1 = _value(ws, "9,500", "השווי למ\"ר")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "היחס לסף": {"related": [v1], "calculation": True}}))
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    assert outcomes[1]["limitation"] == "calculation_incomplete" and "לא נמצא" not in final.answer_markdown


# --- a model-declared "not found" is checked against the workspace (R21) -----------------------------------------

def test_a_value_found_but_written_as_not_found_is_removed_and_stated_with_the_right_reason():
    ws = _ws()
    v1 = _value(ws, "12", "דמי הניהול")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי הניהול לא נמצאו בחיפוש.")
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": [v1]}}, missing_status="missing"))
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    assert 1 in r.removed_units()
    final, outcomes = _finish(ws, a, r, turn)
    assert "דמי הניהול לא נמצאו" not in final.answer_markdown and NOT_FOUND not in final.answer_markdown
    assert final.answer_markdown.startswith("**דמי הניהול**: נמצאו נתונים")
    assert outcomes[1]["limitation"] != "not_found"


def test_a_server_not_found_sentence_for_a_requirement_whose_value_was_found_is_withdrawn():
    ws = _ws()
    v1 = _value(ws, "12", "דמי הניהול")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].", requested=[
        Requested(label="דמי הניהול", document_ids=[], status="not_found_search", checked_where="")])
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": [v1]}}))
    r = verify_answer(p, a, ws, "?", [], statements=coverage.planned_statements(ws, a), requirements=turn)
    assert r.withdrawn
    final, _ = _finish(ws, a, r, turn)
    assert NOT_FOUND not in final.answer_markdown and final.answer_markdown.count("דמי הניהול") == 1


def test_a_requirement_stated_missing_by_a_server_sentence_gets_no_second_sentence():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].", requested=[
        Requested(label="דמי הניהול", document_ids=[], status="not_found_search", checked_where="")])
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": ["H1"]}}))
    r = verify_answer(p, a, ws, "?", [], statements=coverage.planned_statements(ws, a), requirements=turn)
    assert not r.withdrawn and r.ok
    final, outcomes = _finish(ws, a, r, turn)
    assert final.answer_markdown.count("דמי הניהול") == 1 and outcomes[1]["stated"] is True


# --- the other limitation reasons, computed from the workspace (R21) --------------------------------------------

def _missing_with(ws: Workspace, turn: TurnRequirements, related: list[str], status: str = "missing"):
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": related}}, missing_status=status))
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    return _finish(ws, a, r, turn)


def test_an_undeterminable_requirement_is_stated_as_insufficient_to_conclude():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    final, outcomes = _missing_with(ws, TurnRequirements(), ["H1"], status="undeterminable")
    assert outcomes[1]["status"] == "undeterminable" and outcomes[1]["limitation"] == "insufficient"
    assert final.answer_markdown.startswith("**דמי הניהול**: המסמכים אינם מספיקים כדי להכריע בכך.")


def test_a_tool_failure_is_the_reason():
    ws = _ws()
    turn = TurnRequirements()
    turn.record("search", json.dumps({"query": "דמי ניהול"}), "שגיאה: הכלי נכשל. אפשר לנסות שוב או לנסות דרך אחרת")
    final, outcomes = _missing_with(ws, turn, ["E1"])
    assert outcomes[1]["limitation"] == "tool_failure" and "תקלה" in final.answer_markdown


def test_conflicting_values_are_insufficient_to_conclude_naming_the_conflict():
    ws = _ws()
    ids = [_value(ws, "12", "דמי הניהול", "management_fee"), _value(ws, "15", "דמי הניהול", "management_fee")]
    final, outcomes = _missing_with(ws, TurnRequirements(), ids)
    assert outcomes[1]["limitation"] == "insufficient" and "סותרים" in final.answer_markdown


def test_an_uncertain_value_is_found_but_uncertain():
    ws = _ws()
    final, outcomes = _missing_with(ws, TurnRequirements(), [_value(ws, "12", "דמי הניהול", asserted=True)])
    assert outcomes[1]["limitation"] == "uncertain" and "לא נמצא" not in final.answer_markdown


def test_a_document_read_only_in_part_is_found_but_uncertain_naming_the_partial_reading():
    ws = _ws()
    doc = str(uuid.uuid4())
    ws.activity[doc] = {"title": "דוח הדגמה", "level": "read", "read": True, "partial": False, "read_partial": True,
                        "openings": [{"sid": "S1", "scope": "section", "name": "דמי ניהול"}]}
    ws.sources["S1"].document_id = doc
    final, outcomes = _missing_with(ws, TurnRequirements(), ["S1"])
    assert outcomes[1]["limitation"] == "uncertain" and "\"דוח הדגמה\" נקרא רק בחלקו" in final.answer_markdown


# --- correctness and completeness, reported apart (R19) ----------------------------------------------------------

def test_a_complete_verified_answer_reports_full_completeness_and_nothing_missing():
    ws = _ws()
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    r = verify_answer(_judge(_by_words({"השווי למ\"ר": {}})), a, ws, "?", [], requirements=turn)
    final, _ = _finish(ws, a, r, turn)
    counts = r.counts()
    assert counts["correctness"] == "verified" and final.status == "answered"
    assert counts["completeness"] == {"status": "full", "requirements": 1, "missing": []}


def test_a_partial_answer_lists_each_missing_requirement_with_its_reason():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 70 ₪ למ\"ר לחודש [S1].")  # 70 is not in the source
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": ["H1"]}}))
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    _finish(ws, a, r, turn)
    counts = r.counts()
    assert counts["correctness"] == "partial"
    assert counts["completeness"]["status"] == "partial"
    assert [(m["id"], m["reason"]) for m in counts["completeness"]["missing"]] == [("Q2", "not_found")]


def test_overall_completeness_status():
    def outcome(status):
        return {"id": "Q1", "text": "א", "status": status, "limitation": None if status == "full" else "not_found"}

    assert coverage.completeness([]) is None
    assert coverage.completeness([outcome("full")])["status"] == "full"
    assert coverage.completeness([outcome("full"), outcome("missing")])["status"] == "partial"
    assert coverage.completeness([outcome("missing"), outcome("missing")])["status"] == "missing"
    assert coverage.completeness([outcome("undeterminable")])["status"] == "undeterminable"
    assert coverage.completeness([outcome("partial")])["status"] == "partial"


# --- the answer reads naturally after removals (R22) -------------------------------------------------------------

def test_after_removing_one_of_three_bullets_no_orphan_marker_or_duplicate_absence_line_remains():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    a = _answer("- השווי למ\"ר הוא 9,500 ₪ [S1].\n- דמי השכירות הם 70 ₪ למ\"ר לחודש [S1].\n- הנכס פנוי [S1].",
                requested=[Requested(label="דמי הניהול", document_ids=[], status="not_found_search",
                                     checked_where=""),
                           Requested(label="דמי הניהול", document_ids=[], status="not_found_search",
                                     checked_where="")])
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": ["H1"]}}))
    r = verify_answer(p, a, ws, "?", [], statements=coverage.planned_statements(ws, a), requirements=turn)
    final, _ = _finish(ws, a, r, turn)
    lines = [ln.strip() for ln in final.answer_markdown.split("\n") if ln.strip()]
    assert not [ln for ln in lines if not re.search(r"[א-ת0-9]", ln)]
    assert len(lines) == len(set(lines)) and final.answer_markdown.count("דמי הניהול") == 1
    assert "- השווי למ\"ר הוא 9,500 ₪ [S1]." in lines and "- הנכס פנוי [S1]." in lines


def test_tidy_drops_orphan_markers_and_repeated_lines_but_keeps_tables():
    md = ("**דמי הניהול** לא נמצא בחיפוש במסמכים שנבדקו.\n\n- \n* **\n1.\n>\nטקסט [S1].\n"
          "**דמי הניהול** לא נמצא בחיפוש במסמכים שנבדקו.\n\n| א | ב |\n|---|---|\n| 1 | 2 |")
    out = coverage.tidy(md)
    assert out == ("**דמי הניהול** לא נמצא בחיפוש במסמכים שנבדקו.\n\nטקסט [S1].\n\n| א | ב |\n|---|---|\n| 1 | 2 |")
