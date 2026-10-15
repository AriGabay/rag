"""Component coverage and one gap owner (round 7 U3: KTD2, KTD3, KTD4; R4, R6–R11), over the requirements of
round 6 (U9, KTD7, R18–R22).

What the request requires is frozen before the answer (its components) or, without components, derived by the
first judge call (the answer's declared parts are hints only); every judge call scores it by id. Each component's
status (full, partial, not answered, needs clarification, not relevant) is recomputed from the units that survived
verification, a parent's from its children. A component not given is stated once, by the server, in one gap
paragraph after the answer, grouped by reason; the reason is chosen from what the turn did (searches, readings and
their scope, values and their status, conflicts, failures, missing details, unmet instructions, removals) — never
"not found in the search" when its values were found, and never "not present" when no search covered it. A model's
own absence sentence about a component the server states is removed. The judge here is scripted: these tests prove
what the server does with its requirements and scores, not the model's judgement. Synthetic text and values only."""

from __future__ import annotations

import json
import re
import uuid
from decimal import Decimal

from app.chat import calc, coverage, verify
from app.chat.engine import FinalAnswer, Part, Requested
from app.chat.tools import READ_TO_END, Workspace
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
           document=None, asserted: bool = False, grounded: bool = False) -> str:
    """``grounded``: a value whose kind, unit, period and VAT the source states (round 13 U4: the conflict check reads the
    timeline's classes, whose key counts only a field the source states), with the stance of the appraisal's own data."""
    vid = f"V{len(ws.values) + 1}"
    prov = {"unit": "model_asserted" if asserted else "source"}
    if grounded:
        prov |= {"kind": "source", "period": "source", "vat": "source", "stance": "source", "scenario": "not_stated"}
    ws.values[vid] = calc.Value(vid, Decimal(written.replace(",", "")), written, "S1", document or uuid.uuid4(),
                                uuid.uuid4(), None, "דוח בדיקה", "סעיף \"סיכום\"", label, kind, "ILS",
                                "month" if grounded and kind == "management_fee" else "none",
                                "included" if grounded else "unknown", "", subject, "other", prov,
                                {"quote": f"{label} {written}"}, f"{label} {written}",
                                **({"stance": "adopted"} if grounded else {}))
    return vid


def _opened(ws: Workspace, name: str = "תיאור הנכס", *, complete: bool = True, unread: bool = False) -> str:
    """S1's document with S1 as an opened section, read to its end (or not), with an unread region (or not)."""
    doc = str(ws.sources["S1"].document_id)
    target = ("section", doc, name)
    ws.activity[doc] = {"title": "דוח הדגמה", "level": "read", "read": True, "partial": False, "read_partial": False,
                        "read_complete": complete and not unread,
                        "openings": [{"sid": "S1", "scope": "section", "name": name, "target": target}]}
    ws.reads[target] = {"to": READ_TO_END if complete else 3, "unread": unread, "document_id": doc}
    return doc


def _answer(markdown: str, *, requested: list | None = None, parts: list | None = None) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=requested or [], parts=parts or [])


def _req(text: str = "", status: str = "full", units=(), related=(), id: str = "", calculation: bool = False,
         absent=()) -> dict:
    return {"id": id, "text": text, "calculation": calculation, "status": status, "units": list(units),
            "related": list(related), "reason": "בדיקה", "absent": list(absent)}


def _judge(score, rule=lambda text: "supported", seen: list | None = None) -> ScriptedProvider:
    """A judge answering each unit by ``rule(text)`` and the requirements by ``score(frozen, units)``: ``frozen`` is
    None on the call that derives them, else [(id, text)]; ``units`` is {index: text} of the call."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        units = {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}
        frozen = None if "<derive_requirements>" in input else re.findall(
            r'<requirement id="(N[\d.]+)"[^>]*>\n(.*?)\n</requirement>', input, re.S)
        out = {"verdicts": [{"index": i, "verdict": rule(t), "reason": "בדיקה"} for i, t in units.items()]}
        if "<derive_requirements>" in input or "<requirements>" in input:
            out["requirements"] = score(frozen, units)
        return out

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _by_words(asks: dict[str, dict], missing_status: str = "missing"):
    """Score each ask: ``full`` with the units that name it, else ``missing_status`` (with ``related`` from
    ``asks[ask]``, and as its units the units that name it as not found)."""
    def score(frozen, units):
        out = []
        names = [t for _, t in frozen] if frozen is not None else list(asks)
        for n, ask in enumerate(names):
            hits = [i for i, t in units.items() if ask in t and "לא נמצא" not in t]
            said = [i for i, t in units.items() if ask in t and i not in hits]
            extra = asks.get(ask) or {}
            out.append(_req(ask, "full" if hits else missing_status, hits or said, extra.get("related", ()),
                            frozen[n][0] if frozen is not None else "", extra.get("calculation", False)))
        return out
    return score


def _finish(ws: Workspace, answer: FinalAnswer, report, turn: TurnRequirements) -> tuple[FinalAnswer, list[dict]]:
    """What the engine does with a verified answer (``engine.finish``): removals, the components recomputed from the
    surviving units, the one gap paragraph, completeness."""
    final, outcomes = coverage.state_components(ws, answer, report, turn)
    report.completeness = coverage.completeness(outcomes)
    return final, outcomes


def _gap(final: FinalAnswer, answer: FinalAnswer) -> str:
    """The server's gap paragraph: what follows the answer as it was written."""
    body = answer.answer_markdown.strip()
    assert final.answer_markdown.startswith(body), final.answer_markdown
    return final.answer_markdown[len(body):].strip()


def _turn(*components: dict) -> TurnRequirements:
    """The request's components as the analysis returns them (the model's own labels), frozen by the server
    (``request.freeze``: ids ``N1``, ``N1.2``; a child of a conditional component is conditional) and adopted as the
    turn's requirements before the answer."""
    from app.chat import request as request_analysis

    items, _ = request_analysis.freeze([request_analysis.Component(**({"kind": "information"} | c))
                                        for c in components], ["?"])
    turn = TurnRequirements()
    turn.adopt(items, "analysis")
    return turn


def _scores(table: dict[str, dict]):
    """Score frozen ids from ``table[id]`` ({"status", "units", "related", "absent"}); an id not in it is missing."""
    def score(frozen, units):
        return [_req("", **({"status": "missing"} | table.get(i, {})), id=i) for i, _ in frozen]
    return score


# --- requirements come from the judge, not from the answer's own parts (R18, KTD7) ------------------------------

def test_an_answer_that_declares_no_parts_still_gets_requirements_and_a_missing_one_is_stated_after_it():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    seen: list[str] = []
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": ["H1"]}}), seen=seen)
    r = verify_answer(p, a, ws, "מה השווי למ\"ר ומה דמי הניהול?", [], requirements=turn)
    assert "<derive_requirements>" in seen[0] and "<workspace>" in seen[0] and "H1" in seen[0]
    assert [(x["id"], x["text"]) for x in turn.items] == [("N1", "השווי למ\"ר"), ("N2", "דמי הניהול")]
    final, outcomes = _finish(ws, a, r, turn)
    assert final.status == "partial"
    assert _gap(final, a) == "**דמי הניהול** לא נמצא בחיפוש במסמכים שנבדקו."
    assert [(o["id"], o["status"], o["limitation"]) for o in outcomes] == [
        ("N1", "full", None), ("N2", "not_answered", "not_located")]


def test_the_declared_parts_are_hints_in_the_deriving_call_only():
    ws = _ws()
    turn = TurnRequirements()
    seen: list[str] = []
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].", parts=[Part(ask="השווי למ\"ר", answered=True, missing_kind="none")])
    p = _judge(_by_words({"השווי למ\"ר": {}}), seen=seen)
    verify_answer(p, a, ws, "?", [], requirements=turn)
    verify_answer(p, a, ws, "?", [], requirements=turn)
    assert "<hint>" in seen[0] and "<hint>" not in seen[1] and "<derive_requirements>" not in seen[1]
    assert '<requirement id="N1"' in seen[1]


def test_requirements_derived_in_the_first_batch_are_scored_by_id_in_the_next_batch_and_on_a_rejudge(monkeypatch):
    monkeypatch.setattr(verify, "JUDGE_MAX_UNITS", 1)
    ws = _ws()
    turn = TurnRequirements()
    seen: list[str] = []
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי השכירות": {}}), seen=seen)
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    assert len(seen) == 2 and "<derive_requirements>" in seen[0]
    assert '<requirement id="N1"' in seen[1] and '<requirement id="N2"' in seen[1]
    # the second requirement is missing in the first batch and given in the second: merged by id
    assert [(o["id"], o["status"]) for o in r.requirement_outcomes()] == [("N1", "full"), ("N2", "full")]
    again = verify_answer(p, a, ws, "?", [], requirements=turn)
    assert "<derive_requirements>" not in seen[2] and [x["id"] for x in turn.items] == ["N1", "N2"]
    assert [(o["id"], o["status"]) for o in again.requirement_outcomes()] == [("N1", "full"), ("N2", "full")]


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


def test_the_policy_and_schema_carry_the_scores_the_related_ids_and_the_absence_units():
    assert set(verify.REQUIREMENT_STATUSES) == {"full", "partial", "missing", "undeterminable"}
    assert set(verify.COMPONENT_STATUSES) == {"full", "partial", "not_answered", "needs_clarification",
                                              "not_relevant"}
    fields = verify.JudgeRequirement.model_fields
    assert {"id", "text", "status", "units", "related", "calculation", "absent", "aspect"} <= set(fields)
    assert "undeterminable" in verify.JUDGE_REQUIREMENTS_POLICY and "related" in verify.JUDGE_REQUIREMENTS_POLICY
    assert "absent" in verify.JUDGE_REQUIREMENTS_POLICY
    # one reason vocabulary (R8): fourteen reasons, the server's only gap words (``document_not_read``: round 9
    # U6; ``limit_reached`` and ``unit_not_located``: round 9 U16; ``standard_absent``: round 10 U8)
    assert set(coverage.REASONS) == {"not_located", "not_in_part_read", "region_not_read", "not_verifiable",
                                     "sources_conflict", "detail_missing", "calculation_incomplete", "tool_failure",
                                     "instruction_not_met", "removed", "document_not_read", "limit_reached",
                                     "unit_not_located", "standard_absent"}
    assert not hasattr(coverage, "LIMITATIONS") and not hasattr(coverage, "ABSENCE_KINDS")


def test_a_requirement_no_call_scored_is_never_taken_as_given():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")

    def score(frozen, units):  # derives two requirements and scores only the first
        if frozen:
            return [_req(status="full", units=list(units), id=frozen[0][0])]
        return [_req("השווי למ\"ר", "full", list(units)), _req("דמי הניהול", "full", [])]

    verify_answer(_judge(score), a, ws, "?", [], requirements=turn)  # derived: the second given by no unit
    r = verify_answer(_judge(score), a, ws, "?", [], requirements=turn)  # re-judged: the second is not scored
    assert [(o["id"], o["status"]) for o in r.requirement_outcomes()] == [("N1", "full"), ("N2", "not_answered")]


# --- coverage from the units that survived verification (R6, R7, KTD3) -------------------------------------------

def test_a_component_filled_only_by_a_removed_claim_falls_back_to_not_answered_removed_in_verification():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי הניהול הם 9,500 ₪ [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": ["H1"]}}),
               rule=lambda t: "unsupported" if "ניהול" in t else "supported")
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    assert outcomes[1]["status"] == "not_answered" and outcomes[1]["limitation"] == "removed"
    assert outcomes[1]["units"] == [] and outcomes[1]["removed_units"] == [1]
    assert "**דמי הניהול**" in final.answer_markdown and "הוסר באימות" in final.answer_markdown
    assert NOT_FOUND not in final.answer_markdown and final.status == "partial"


def test_a_component_filled_by_two_claims_one_removed_stays_full():
    ws = _ws()
    turn = _turn({"id": "1", "text": "השווי למ\"ר ומצב האכלוס"})
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס מושכר [S1].")
    r = verify_answer(_judge(_scores({"N1": {"status": "full", "units": [0, 1]}}),
                             rule=lambda t: "unsupported" if "מושכר" in t else "supported"), a, ws, "?", [],
                      requirements=turn)
    assert r.removed_units() == {1}
    final, outcomes = _finish(ws, a, r, turn)
    assert [(o["id"], o["status"], o["units"], o["removed_units"]) for o in outcomes] == [("N1", "full", [0], [1])]
    assert "הוסר באימות" not in final.answer_markdown


def test_one_claim_filling_two_components_counts_for_both():
    ws = _ws()
    turn = _turn({"id": "1", "text": "השווי למ\"ר"}, {"id": "2", "text": "דמי השכירות"})
    a = _answer("השווי למ\"ר הוא 9,500 ₪ ודמי השכירות הם 55 ₪ למ\"ר לחודש [S1].")
    r = verify_answer(_judge(_scores({"N1": {"status": "full", "units": [0]}, "N2": {"status": "full", "units": [0]}})),
                      a, ws, "?", [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    assert [(o["id"], o["status"], o["units"]) for o in outcomes] == [("N1", "full", [0]), ("N2", "full", [0])]
    assert final.answer_markdown == a.answer_markdown and final.status == "answered"


def test_a_parent_takes_its_status_from_its_children_and_only_the_missing_children_are_stated():
    # AE2: six conditional items under one category; the source shows three, its section was read to the end
    ws = _ws("תכנית: מגורים ומסחר. 84 יח\"ד. 9 קומות.")
    _opened(ws, "מצב תכנוני")
    items = ["שימושים", "יח\"ד", "שטחים", "גובה", "קומות", "קווי בניין"]
    turn = _turn({"id": "1", "text": "נתוני התכנית", "conditional": True},
                 *({"id": f"1.{n}", "text": t, "parent": "1"} for n, t in enumerate(items, 1)))
    a = _answer("הייעוד מגורים ומסחר [S1].\nמספר יחידות הדיור 84 יח\"ד [S1].\nמספר הקומות 9 קומות [S1].")
    given = {"N1.1": [0], "N1.2": [1], "N1.5": [2]}
    # the judge scores the category as a whole missing (as the trace found), each item by its units
    table = {"N1": {"status": "missing", "related": ["S1"]},
             **{f"N1.{n}": ({"status": "full", "units": given[f"N1.{n}"]} if f"N1.{n}" in given
                            else {"status": "missing", "related": ["S1"]}) for n in range(1, 7)}}
    r = verify_answer(_judge(_scores(table)), a, ws, "?", [], requirements=turn)
    assert not [p for p in r.problems if p.kind == "requirement"]  # read and checked: nothing to search
    final, outcomes = _finish(ws, a, r, turn)
    by_id = {o["id"]: o for o in outcomes}
    assert by_id["N1"]["status"] == "partial" and by_id["N1"]["limitation"] is None
    assert by_id["N1.3"]["conditional"] and by_id["N1.3"]["limitation"] == "not_in_part_read"
    gap = _gap(final, a)
    # one sentence for the three missing items, naming the section read; nothing for the category or what was given
    assert gap == "**שטחים**, **גובה** ו**קווי בניין** לא מופיעים בסעיף \"מצב תכנוני\" שנבדק [S1]."
    assert not [w for w in ("נתוני התכנית", "שימושים", "קומות", "יח\"ד") if w in gap]


def test_the_gap_paragraph_lists_only_unmet_components_grouped_by_reason_without_restating_the_request():
    ws = _ws()
    ws.searches.append("שטח")
    turn = TurnRequirements()
    turn.record("search", json.dumps({"query": "חניה"}), "שגיאה: הכלי נכשל. אפשר לנסות שוב או לנסות דרך אחרת")
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "שטח המגרש": {"related": ["H1"]}, "שטח הבנייה": {"related": ["H1"]},
                          "מספר החניות": {"related": ["E1"]}}))
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    lines = _gap(final, a).split("\n")
    # one line per reason, in the order of the reason table (a failure before "not located")
    assert lines == ["**מספר החניות** לא הושלם בגלל תקלה בכלי או בשירות המודל בזמן הבדיקה.",
                     "**שטח המגרש** ו**שטח הבנייה** לא נמצאו בחיפוש במסמכים שנבדקו."]
    assert "השווי למ\"ר" not in _gap(final, a)
    assert [o["stated"] for o in outcomes] == [False, True, True, True]


# --- a missing requirement whose data exist is a repair problem (R20, AE6); an instruction never is a search ------

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


def test_a_requirement_no_search_covered_is_a_repair_problem_and_then_stated_as_not_located_by_any_search():
    ws = _ws()
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    r = verify_answer(_judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {}})), a, ws, "?", [], requirements=turn)
    assert [x.kind for x in r.problems] == ["requirement"] and "לחפש" in r.problems_text()
    final, outcomes = _finish(ws, a, r, turn)
    gap = _gap(final, a)
    # "not located by the searches performed", saying no search covered it — never "not found" or "not present"
    assert gap.startswith("**דמי הניהול** לא אותר בחיפושים שבוצעו: אף חיפוש או קריאה לא כיסו אותו")
    assert "לא נמצא" not in gap and "לא מופיע" not in gap
    assert outcomes[1]["limitation"] == "not_located" and outcomes[1]["searched"] is False


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
    assert _gap(final, a) == "**היחס לסף**: החישוב נכשל על הנתונים שנמצאו, ולכן התוצאה לא הושלמה."
    assert outcomes[1]["limitation"] == "calculation_incomplete"
    # the completeness line names the gap and its status; the reason is said once, in the answer
    assert r.counts()["completeness"]["missing"] == [{
        "id": "N2", "text": "היחס לסף", "status": "not_answered", "reason": "calculation_incomplete", "parent": "",
        "conditional": False, "missing_part": ""}]


def test_found_inputs_of_a_calculation_never_computed_say_the_calculation_was_not_completed():
    ws = _ws()
    v1 = _value(ws, "9,500", "השווי למ\"ר")
    turn = TurnRequirements()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    p = _judge(_by_words({"השווי למ\"ר": {}, "היחס לסף": {"related": [v1], "calculation": True}}))
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    assert outcomes[1]["limitation"] == "calculation_incomplete" and "לא נמצא" not in final.answer_markdown
    assert "הנתונים לחישוב נמצאו, אבל החישוב לא הושלם" in final.answer_markdown


# --- one owner per gap: a model's absence sentence gives way to the server's (R9, KTD4, AE4) ---------------------

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
    assert final.answer_markdown.count("דמי הניהול") == 1 and "**דמי הניהול**: נמצאו נתונים" in final.answer_markdown
    assert outcomes[1]["limitation"] == "not_verifiable"


def test_ae4_found_values_with_a_failed_calculation_replace_the_models_not_found_sentence_with_one_statement():
    ws = _ws("סיכום: השווי למ\"ר הוא 9,500 ₪. סף ההשוואה הוא 8,000 ₪ למ\"ר.")
    v1, v2 = _value(ws, "9,500", "השווי למ\"ר"), _value(ws, "8,000", "סף ההשוואה")
    turn = _turn({"id": "1", "text": "השווי למ\"ר"}, {"id": "2", "text": "היחס לסף", "kind": "calculation"})
    turn.record("calculate", json.dumps({"expression": "V1 / V2", "label": "היחס לסף"}),
                f"שגיאה: אי אפשר לחשב: היחידות אינן מתאימות (V1 / V2). {calc.MSG_FAILED}")
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהיחס לסף לא נמצא בחיפוש במסמכים.")
    # the judge marks the model's sentence as an absence of N2 (``absent``), and finds it not factual
    p = _judge(_scores({"N1": {"status": "full", "units": [0]},
                        "N2": {"status": "missing", "related": [v1, v2, "F1"], "absent": [1]}}),
               rule=lambda t: "not_factual" if "לא נמצא" in t else "supported")
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    md = final.answer_markdown
    assert NOT_FOUND not in md
    (about,) = [line for line in md.splitlines() if "היחס לסף" in line]
    assert about == "**היחס לסף**: החישוב נכשל על הנתונים שנמצאו, ולכן התוצאה לא הושלמה."
    assert outcomes[1]["absence_units"] == [1] and 1 in r.removed_units()
    assert r.counts()["removed"] == 0  # replaced by the server's statement, not a claim that failed


def test_an_undeterminable_component_keeps_the_cited_value_the_hedging_answer_gave():
    ws = _ws()
    turn = _turn({"id": "1", "text": "השווי למ\"ר"})
    a = _answer("בסיכום נכתב כי השווי למ\"ר הוא 9,500 ₪ [S1].\nאין במקורות הבהרה אם סכום זה אומץ כקביעה הסופית.")
    # the judge names both the value and the hedge as the units of an undeterminable component
    p = _judge(_scores({"N1": {"status": "undeterminable", "units": [0, 1]}}),
               rule=lambda t: "not_factual" if "אין במקורות" in t else "supported")
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    md = final.answer_markdown
    assert 0 not in r.removed_units()  # the supported value is not an absence statement: it stays
    assert "9,500" in md and "[S1]" in md
    assert outcomes[0]["evidence_state"] == "undeterminable"
    assert "**השווי למ\"ר**: המסמכים אינם מספיקים כדי להכריע בכך." in md


def test_the_backstop_removes_an_unmarked_uncited_not_found_sentence_about_a_component_the_server_states():
    ws = _ws("סיכום: השווי למ\"ר הוא 9,500 ₪. סף ההשוואה הוא 8,000 ₪ למ\"ר.")
    v1, v2 = _value(ws, "9,500", "השווי למ\"ר"), _value(ws, "8,000", "סף ההשוואה")
    turn = _turn({"id": "1", "text": "השווי למ\"ר"}, {"id": "2", "text": "היחס לסף", "kind": "calculation"})
    turn.record("calculate", json.dumps({"expression": "V1 / V2", "label": "היחס לסף"}),
                f"שגיאה: אי אפשר לחשב: היחידות אינן מתאימות (V1 / V2). {calc.MSG_FAILED}")
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהיחס לסף לא נמצא בחיפוש במסמכים.")
    # the judge neither links nor marks the sentence: it passes as not factual
    p = _judge(_scores({"N1": {"status": "full", "units": [0]}, "N2": {"status": "missing",
                                                                    "related": [v1, v2, "F1"]}}),
               rule=lambda t: "not_factual" if "לא נמצא" in t else "supported")
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    assert not r.removed_units()  # verification kept it
    final, _ = _finish(ws, a, r, turn)
    assert NOT_FOUND not in final.answer_markdown
    assert [line for line in final.answer_markdown.splitlines() if "היחס לסף" in line] == [
        "**היחס לסף**: החישוב נכשל על הנתונים שנמצאו, ולכן התוצאה לא הושלמה."]


def test_a_not_found_sentence_is_kept_when_the_server_states_no_gap():
    ws = _ws()
    turn = _turn({"id": "1", "text": "השווי למ\"ר"})
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nנתון על חניה לא נמצא בקטע שצוטט.")
    p = _judge(_scores({"N1": {"status": "full", "units": [0]}}),
               rule=lambda t: "not_factual" if "חניה" in t else "supported")
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    final, _ = _finish(ws, a, r, turn)
    assert final.answer_markdown == a.answer_markdown


# --- each reason comes from its evidence (R8, KTD4) ---------------------------------------------------------------

def _missing_with(ws: Workspace, turn: TurnRequirements, related: list[str], status: str = "missing",
                  requested: list | None = None):
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].", requested=requested)
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": related}}, missing_status=status))
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    return final, outcomes, _gap(final, a)


def test_a_search_with_no_hits_gives_not_located_by_the_searches():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ["H1"])
    assert outcomes[1]["limitation"] == "not_located" and outcomes[1]["searched"] is True
    assert gap == "**דמי הניהול** לא נמצא בחיפוש במסמכים שנבדקו."


def test_a_section_read_to_its_end_without_the_value_gives_not_present_in_that_section():
    ws = _ws()
    _opened(ws)
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ["S1"])
    assert outcomes[1]["limitation"] == "not_in_part_read" and outcomes[1]["place"]["sid"] == "S1"
    assert gap == "**דמי הניהול** לא מופיע בסעיף \"תיאור הנכס\" שנבדק [S1]."


def test_the_documents_a_gap_statement_names_are_in_the_ledger_for_the_permission_check():
    ws = _ws()
    doc = _opened(ws)
    _, outcomes, _ = _missing_with(ws, TurnRequirements(), ["S1"])
    assert coverage.gap_documents(outcomes) == [{"document_id": doc, "title": "דוח הדגמה"}]
    assert coverage.ledger_documents({"gap_documents": coverage.gap_documents(outcomes)}) == {doc}


def test_a_section_with_an_unread_region_gives_not_fully_read():
    ws = _ws()
    _opened(ws, unread=True)
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ["S1"])
    assert outcomes[1]["limitation"] == "region_not_read"
    assert "אזורים שלא נקראו" in gap and "לא מופיע" not in gap


def test_a_section_read_only_in_part_gives_not_fully_read():
    ws = _ws()
    _opened(ws, complete=False)
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ["S1"])
    assert outcomes[1]["limitation"] == "region_not_read" and "נקרא רק בחלקו" in gap


def test_a_document_read_only_in_part_gives_not_fully_read_naming_the_document():
    ws = _ws()
    doc = str(uuid.uuid4())
    ws.activity[doc] = {"title": "דוח הדגמה", "level": "read", "read": True, "partial": False, "read_partial": True,
                        "openings": [{"sid": "S1", "scope": "section", "name": "דמי ניהול"}]}
    ws.sources["S1"].document_id = doc
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ["S1"])
    assert outcomes[1]["limitation"] == "region_not_read" and "\"דוח הדגמה\" נקרא רק בחלקו" in gap


def test_an_uncertain_value_gives_found_not_verifiable():
    ws = _ws()
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), [_value(ws, "12", "דמי הניהול", asserted=True)])
    assert outcomes[1]["limitation"] == "not_verifiable" and "לא נמצא" not in gap and "אינן ודאיות" in gap


def test_an_undeterminable_score_is_an_evidence_state_not_a_status():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    final, outcomes, gap = _missing_with(ws, TurnRequirements(), ["H1"], status="undeterminable")
    assert outcomes[1]["status"] == "not_answered" and outcomes[1]["evidence_state"] == "undeterminable"
    assert outcomes[1]["limitation"] == "not_verifiable"
    assert gap == "**דמי הניהול**: המסמכים אינם מספיקים כדי להכריע בכך."


def test_a_tool_failure_is_the_reason():
    ws = _ws()
    turn = TurnRequirements()
    turn.record("search", json.dumps({"query": "דמי ניהול"}), "שגיאה: הכלי נכשל. אפשר לנסות שוב או לנסות דרך אחרת")
    _, outcomes, gap = _missing_with(ws, turn, ["E1"])
    assert outcomes[1]["limitation"] == "tool_failure" and "תקלה" in gap


def test_two_values_for_the_same_property_conflict():
    ws = _ws()
    doc = uuid.uuid4()  # round 13 U4: one datum is the timeline's class, so the two values must be of one identity (one unit)
    ids = [_value(ws, "12", "דמי הניהול", "management_fee", document=doc, grounded=True),
           _value(ws, "15", "דמי הניהול", "management_fee", document=doc, grounded=True)]
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ids)
    assert outcomes[1]["limitation"] == "sources_conflict" and "סותרים" in gap


def test_two_values_for_different_properties_or_scenarios_do_not_conflict():
    ws = _ws()
    ids = [_value(ws, "12", "דמי הניהול", "management_fee"),
           _value(ws, "15", "דמי הניהול", "management_fee", subject="נכס השוואה")]
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ids)
    assert outcomes[1]["limitation"] == "not_verifiable" and "סותרים" not in gap
    ws = _ws()
    doc = uuid.uuid4()  # round 13 U4: one identity, so only the scenario (a stated one) tells the two apart
    ids = [_value(ws, "12", "דמי הניהול", "management_fee", document=doc, grounded=True),
           _value(ws, "15", "דמי הניהול", "management_fee", document=doc, grounded=True)]
    assert coverage.conflicting(ws)
    ws.values[ids[1]].scenario = "לאחר מימוש התכנית"
    ws.values[ids[1]].provenance["scenario"] = "source"
    assert not coverage.conflicting(ws)


def test_a_parameter_the_user_did_not_give_gives_detail_missing_from_the_request():
    ws = _ws()
    v1 = _value(ws, "9,500", "העלויות")
    turn = _turn({"id": "1", "text": "השווי למ\"ר"},
                 {"id": "2", "text": "הרווח אם העלויות יעלו", "kind": "calculation",
                  "parameters": [{"name": "שיעור העלייה", "source": "not_given_by_user"}]})
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    r = verify_answer(_judge(_scores({"N1": {"status": "full", "units": [0]}, "N2": {"related": [v1]}})), a, ws, "?",
                      [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    assert outcomes[1]["limitation"] == "detail_missing"
    assert _gap(final, a) == "**הרווח אם העלויות יעלו**: חסר פרט שהמשתמש צריך לתת (שיעור העלייה), ולכן לא נקבעה תוצאה."


def test_a_clarification_component_needs_clarification():
    ws = _ws()
    turn = _turn({"id": "1", "text": "השווי למ\"ר"}, {"id": "2", "text": "איזה שלב של הפרויקט", "kind": "clarification"})
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    r = verify_answer(_judge(_scores({"N1": {"status": "full", "units": [0]}})), a, ws, "?", [], requirements=turn)
    assert not [p for p in r.problems if p.kind == "requirement"]  # never searched
    _, outcomes = _finish(ws, a, r, turn)
    assert (outcomes[1]["status"], outcomes[1]["limitation"]) == ("needs_clarification", "detail_missing")


# --- the model's per-component claims are validated, and only downgraded (KTD4) ----------------------------------

def test_a_not_found_search_claim_with_no_covering_search_is_downgraded_to_no_search_covered_it():
    ws = _ws()
    claim = Requested(component="N2", label="דמי הניהול", document_ids=[], status="not_found_search",
                      checked_where="")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), [], requested=[claim])
    assert outcomes[1]["limitation"] == "not_located" and outcomes[1]["searched"] is False
    assert outcomes[1]["claimed"] == "not_found_search" and "לא נמצא" not in gap and "לא כיסו" in gap


def test_a_section_claim_validated_against_the_reading_gives_not_present_in_that_section():
    ws = _ws()
    doc = _opened(ws)
    claim = Requested(component="N2", label="דמי הניהול", document_ids=[doc], status="section_checked_absent",
                      checked_where="S1")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), [], requested=[claim])
    assert outcomes[1]["limitation"] == "not_in_part_read" and "[S1]" in gap


def test_a_section_claim_about_a_section_read_in_part_is_downgraded():
    ws = _ws()
    doc = _opened(ws, complete=False)
    claim = Requested(component="N2", label="דמי הניהול", document_ids=[doc], status="section_checked_absent",
                      checked_where="S1")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), [], requested=[claim])
    assert outcomes[1]["limitation"] == "region_not_read" and "לא מופיע" not in gap


def test_a_claim_never_makes_a_component_given_nor_overrides_found_data():
    ws = _ws()
    v1 = _value(ws, "12", "דמי הניהול")
    claims = [Requested(component="N2", label="דמי הניהול", document_ids=[], status="not_found_search",
                        checked_where=""),
              Requested(component="N1", label="השווי למ\"ר", document_ids=[], status="found", checked_where="")]
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), [v1], requested=claims)
    assert outcomes[1]["status"] == "not_answered" and outcomes[1]["limitation"] == "not_verifiable"
    assert NOT_FOUND not in gap


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
    assert [(m["id"], m["reason"]) for m in counts["completeness"]["missing"]] == [("N2", "not_located")]


def test_overall_completeness_status():
    def outcome(status, id="N1", parent=""):
        return {"id": id, "text": "א", "status": status, "parent": parent, "conditional": False, "children": [],
                "limitation": None if status in ("full", "not_relevant") else "not_located"}

    assert coverage.completeness([]) is None
    assert coverage.completeness([outcome("full")])["status"] == "full"
    assert coverage.completeness([outcome("full"), outcome("not_answered", "N2")])["status"] == "partial"
    assert coverage.completeness([outcome("not_answered"), outcome("not_answered", "N2")])["status"] == "missing"
    assert coverage.completeness([outcome("partial")])["status"] == "partial"
    assert coverage.completeness([outcome("full"), outcome("not_relevant", "N2")]) == {
        "status": "full", "requirements": 1, "missing": []}


# --- the answer reads naturally after removals (R22) -------------------------------------------------------------

def test_after_removing_one_of_three_bullets_no_orphan_marker_or_duplicate_absence_line_remains():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    turn = TurnRequirements()
    a = _answer("- השווי למ\"ר הוא 9,500 ₪ [S1].\n- דמי השכירות הם 70 ₪ למ\"ר לחודש [S1].\n- הנכס פנוי [S1].",
                requested=[Requested(component="N2", label="דמי הניהול", document_ids=[], status="not_found_search",
                                     checked_where=""),
                           Requested(component="N2", label="דמי הניהול", document_ids=[], status="not_found_search",
                                     checked_where="")])
    p = _judge(_by_words({"השווי למ\"ר": {}, "דמי הניהול": {"related": ["H1"]}}))
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
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


# --- a removal names its component and its failure kind (round 7 U4: KTD5, R7, R12, AE3) ---------------------------

def _wrong_property(text: str) -> dict:
    return ({"verdict": "unsupported", "failure": "wrong_subject"} if "שכירות" in text else {"verdict": "supported"})


def _structured_judge(score, rule) -> ScriptedProvider:
    """A judge whose verdict on each unit is ``rule(text)`` (a dict of the verdict's fields), with ``score`` as
    ``_judge``'s; the removed units it is shown join the call's units for the scores."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        units = {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}
        removed = {int(i): t for i, t in re.findall(r'<removed_unit index="(\d+)">\n(.*?)\n</removed_unit>', input,
                                                    re.S)}
        frozen = re.findall(r'<requirement id="(N[\d.]+)"[^>]*>\n(.*?)\n</requirement>', input, re.S)
        return {"verdicts": [{"index": i, "reason": "בדיקה"} | rule(t) for i, t in units.items()],
                "requirements": score(frozen, units | removed)}

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def test_ae3_a_claim_citing_another_propertys_value_is_removed_as_wrong_property_and_its_component_is_named():
    ws = _ws()
    turn = _turn({"id": "1", "text": "השווי למ\"ר"}, {"id": "2", "text": "דמי השכירות"})
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות בנכס הם 55 ₪ למ\"ר לחודש [S1].")
    p = _structured_judge(_scores({"N1": {"status": "full", "units": [0]}, "N2": {"status": "full", "units": [1]}}),
                          _wrong_property)
    r = verify_answer(p, a, ws, "?", [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    (decision,) = r.removals()
    assert (decision.failure_kind, decision.component, decision.checked_ids) == ("wrong_subject", "N2", ["S1"])
    rent = outcomes[1]
    assert rent["status"] == "not_answered" and rent["limitation"] == "removed"
    assert rent["removal_kinds"] == ["wrong_subject"]
    assert "דמי השכירות בנכס" not in final.answer_markdown
    (gap,) = [g for g in r.gaps if g["components"] == ["N2"]]
    assert gap["reason"] == "removed" and "**דמי השכירות**" in gap["text"] and "לנכס או לצד אחר" in gap["text"]
    assert gap["text"] in final.answer_markdown
    # round 8 U7: the row also names its claim and the action taken
    assert r.counts()["removals"] == [{"failure_kind": "wrong_subject", "component": "N2",
                                        "text": verify.REMOVAL_SENTENCES["wrong_subject"],
                                        "claim_id": decision.claim_id, "action": "removed"}]


def test_a_claim_the_deterministic_checks_removed_is_shown_to_the_judge_so_its_component_is_known():
    ws = _ws()
    turn = _turn({"id": "1", "text": "השווי למ\"ר"}, {"id": "2", "text": "דמי השכירות"})
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 1,234 ₪ למ\"ר לחודש [S1].")
    seen: list[str] = []

    def score(frozen, units):
        seen.append(json.dumps(units, ensure_ascii=False))
        return [_req("", "full", [i for i, t in units.items() if ask.split()[0] in t], id=n) for n, ask in frozen]

    r = verify_answer(_structured_judge(score, lambda t: {"verdict": "supported"}), a, ws, "?", [], requirements=turn)
    assert "1,234" in seen[0]  # the removed unit was in the call, as removed
    (decision,) = r.removals()
    assert (decision.failure_kind, decision.check, decision.component) == ("absent_from_source", "unstated_number", "N2")
    _, outcomes = _finish(ws, a, r, turn)
    assert [(o["id"], o["status"], o["limitation"], o["removed_units"]) for o in outcomes] == [
        ("N1", "full", None, []), ("N2", "not_answered", "removed", [1])]


# --- a calculation component is full only through a computation (round 7 KTD3, R6, R7) -------------------------------

def _computed(ws: Workspace, expression: str, label: str, reproduces: dict | None = None) -> str:
    """A calculation registered as ``tools.tool_calculate`` registers it (without the database checks)."""
    node = calc.parse(expression)
    ids = list(dict.fromkeys(calc.ids_of(node)))
    out = calc.evaluate(node, {i: ws.values[i].operand() for i in ids})
    cid = f"C{len(ws.computations) + 1}"
    ws.computations[cid] = calc.Computation(cid, label, calc.render(node, lambda i: i), label, out, [
        {"id": i, "label": ws.values[i].label, "kind": "value", "value": str(ws.values[i].value),
         "display": calc.fmt(ws.values[i].value), "source_id": "S1", "value_text": ws.values[i].written}
        for i in out.inputs], ["S1"], 1, "reproduces_report_value" if reproduces else "computed", None, "",
        reproduces, list(out.inputs))
    return cid


FORMULA = "הרווח מחושב כהכנסות פחות העלויות [S1]."
PROFIT_QUESTION = "איך מחושב הרווח הכולל, ומה יקרה אם כל העלויות יעלו ב-5% וההכנסות יישארו קבועות?"
NOT_COMPUTED = "אם כל העלויות יעלו ב-5% וההכנסות יישארו קבועות, הרווח יקטן; הסכום החדש לא חושב כאן [S1]."


def _profit_turn() -> TurnRequirements:
    return _turn({"id": "a", "text": "איך מחושב הרווח הכולל", "kind": "calculation"},
                 {"id": "b", "text": "הרווח אם העלויות יעלו ב-5%", "kind": "calculation"})


def _profit_ws() -> tuple[Workspace, str, str]:
    ws = _ws("סיכום: סה\"כ הכנסות 9,500 ₪, סה\"כ עלויות 8,000 ₪, רווח 1,500 ₪.")
    return ws, _value(ws, "9,500", "סה\"כ הכנסות", "income"), _value(ws, "8,000", "סה\"כ עלויות", "cost")


def test_a_calculation_component_scored_full_without_a_computation_is_partial_and_asks_to_compute():
    ws, v1, v2 = _profit_ws()
    turn = _profit_turn()
    a = _answer(f"{FORMULA} {NOT_COMPUTED}")
    scores = _scores({"N1": {"status": "full", "units": [0], "related": [v1, v2]},
                      "N2": {"status": "full", "units": [1], "related": [v1, v2]}})
    r = verify_answer(_judge(scores), a, ws, PROFIT_QUESTION, [], requirements=turn)
    outcomes = {o["id"]: o for o in r.requirement_outcomes()}
    assert [(o["status"], o.get("not_computed")) for o in outcomes.values()] == [("partial", True), ("partial", True)]
    assert outcomes["N2"]["units"] == [1]  # what fills it is kept, the claims are not removed
    assert not r.removed_units()
    # before the repair decision: the existing "data found — complete it, compute it" problem, per component
    problems = [p for p in r.problems if p.kind == "requirement"]
    assert [p.component for p in problems] == ["N1", "N2"] and not r.ok
    assert all("calculate" in p.reason and v1 in p.reason for p in problems)
    # after the repair bound, the gap's reason is the server's: the calculation was not completed
    final, outcomes = _finish(ws, a, r, turn)
    assert [o["limitation"] for o in outcomes] == ["calculation_incomplete", "calculation_incomplete"]


def test_a_calculation_component_filled_by_a_unit_citing_a_computation_stays_full():
    ws, v1, v2 = _profit_ws()
    cid = _computed(ws, f"{v1} - {v2}", "הרווח")
    turn = _profit_turn()
    a = _answer(f"{FORMULA} הרווח הוא 1,500 ₪ [{cid}].")
    scores = _scores({"N1": {"status": "full", "units": [0, 1], "related": [v1, v2, cid]},
                      "N2": {"status": "full", "units": [1], "related": [cid]}})
    r = verify_answer(_judge(scores), a, ws, "?", [], requirements=turn)
    assert [o["status"] for o in r.requirement_outcomes()] == ["full", "full"]
    assert not [p for p in r.problems if p.kind == "requirement"] and r.ok


def test_a_computation_bound_to_a_unit_that_shows_its_result_without_citing_it_fills_the_component():
    ws, v1, v2 = _profit_ws()
    _computed(ws, f"{v1} - {v2}", "הרווח")
    turn = _turn({"id": "a", "text": "הרווח", "kind": "calculation"})
    a = _answer(f"הרווח הוא 1,500 ₪ [{v1}][{v2}].")
    r = verify_answer(_judge(_scores({"N1": {"status": "full", "units": [0]}})), a, ws, "?", [], requirements=turn)
    assert [o["status"] for o in r.requirement_outcomes()] == ["full"]


def test_a_document_value_a_computation_reproduces_fills_the_component():
    ws, v1, v2 = _profit_ws()
    _computed(ws, f"{v1} - {v2}", "הרווח", reproduces={"source": "S1", "as_written": "1,500"})
    turn = _turn({"id": "a", "text": "הרווח", "kind": "calculation"})
    shown = verify_answer(_judge(_scores({"N1": {"status": "full", "units": [0]}})),
                          _answer("לפי השומה הרווח הוא 1,500 ₪ [S1]."), ws, "?", [], requirements=turn)
    assert [o["status"] for o in shown.requirement_outcomes()] == ["full"]
    # citing the same source for something else is no computation
    other = verify_answer(_judge(_scores({"N1": {"status": "full", "units": [0]}})),
                          _answer("ההכנסות הן 9,500 ₪ [S1]."), ws, "?", [], requirements=turn)
    assert [o["status"] for o in other.requirement_outcomes()] == ["partial"]


def test_an_information_component_or_a_calculation_waiting_for_the_user_keeps_its_path():
    ws, v1, v2 = _profit_ws()
    turn = _turn({"id": "a", "text": "איך מחושב הרווח הכולל"})  # information
    r = verify_answer(_judge(_scores({"N1": {"status": "full", "units": [0]}})), _answer(FORMULA), ws, "?", [],
                      requirements=turn)
    assert [o["status"] for o in r.requirement_outcomes()] == ["full"]
    # a calculation waiting for a detail only the user can give (KTD9) is not sent to compute
    turn = _turn({"id": "a", "text": "הרווח בתרחיש", "kind": "calculation",
                  "parameters": [{"name": "שיעור העלייה", "source": "not_given_by_user", "quote": ""}]})
    r = verify_answer(_judge(_scores({"N1": {"status": "full", "units": [0]}})),
                      _answer("כדי לחשב את הרווח בתרחיש צריך את שיעור העלייה: מה הוא? הרווח מחושב כהכנסות פחות העלויות "
                              "[S1]."), ws, "?", [], requirements=turn)
    assert r.pending_parameters == {"N1": ["שיעור העלייה"]}
    assert [o["status"] for o in r.requirement_outcomes()] == ["full"]
    assert not [p for p in r.problems if p.kind == "requirement"]


# --- completeness from claims (round 8 U3, KTD9, R20, R21, AE7) -------------------------------------------------------

AREAS_ONLY = "תא שטח 1: השטחים העיקריים הם 1,200 מ״ר. נתון נוסף: 300 מ״ר."  # the figure 300 is there, not as service areas
MIXED_AREAS = "השטחים העיקריים בתא שטח 1 הם 1,200 מ״ר, ושטחי השירות בתא שטח 1 הם 300 מ״ר [S1]."
MAIN_CLAIM, SERVICE_CLAIM = "השטחים העיקריים בתא שטח 1 הם 1,200 מ״ר", "ושטחי השירות בתא שטח 1 הם 300 מ״ר"


def _mixed_areas_judge(score_claims: bool = True) -> ScriptedProvider:
    """One sentence holds both areas; the source supports the main area only. The judge splits the unit into its two
    claims and scores each component by the claim that gives it (``claims``)."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        frozen = re.findall(r'<requirement id="(N[\d.]+)"[^>]*>\n(.*?)\n</requirement>', input, re.S)
        out = {"verdicts": [{"index": i, "verdict": "partial", "reason": "חלקי", "defect": "part", "claims": [
            {"span": MAIN_CLAIM, "verdict": "supported"}, {"span": SERVICE_CLAIM, "verdict": "unsupported"}]}
            for i in (int(x) for x in re.findall(r'<unit index="(\d+)"', input))]}
        if "<requirements>" in input:
            by_id = {"N1": ("missing", [], []), "N1.1": ("full", [0], [MAIN_CLAIM]),
                     "N1.2": ("full", [0], [SERVICE_CLAIM] if score_claims else [])}
            out["requirements"] = [{"id": rid, "text": "", "calculation": False, "status": by_id[rid][0],
                                    "units": by_id[rid][1], "claims": by_id[rid][2], "related": [], "reason": "בדיקה"}
                                   for rid, _ in frozen]
        return out

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _areas_turn() -> TurnRequirements:
    return _turn({"id": "1", "text": "השטחים בתא שטח 1"}, {"id": "1.1", "text": "השטחים העיקריים", "parent": "1"},
                 {"id": "1.2", "text": "שטחי השירות", "parent": "1"})


def test_ae7_a_component_given_only_by_a_failing_claim_is_the_one_missing_component_with_its_reason():
    ws = _ws(AREAS_ONLY)
    turn = _areas_turn()
    a = _answer(MIXED_AREAS)
    r = verify_answer(_mixed_areas_judge(), a, ws, "?", [], requirements=turn)
    by_id = {o["id"]: o for o in r.requirement_outcomes()}
    assert (by_id["N1.1"]["status"], by_id["N1.2"]["status"], by_id["N1"]["status"]) == (
        "full", "not_answered", "partial")
    assert by_id["N1.2"]["removal_kinds"] == ["absent_from_source"] and by_id["N1.2"]["removed_units"] == [0]
    _, outcomes = _finish(ws, a, r, turn)
    missing = coverage.completeness(outcomes)["missing"]
    assert [m["id"] for m in missing] == ["N1.2"], missing  # the category and the main areas are not listed
    assert missing[0]["reason"] == "removed"


def test_a_vote_without_claim_spans_counts_the_unit_as_before():
    ws = _ws(AREAS_ONLY)
    r = verify_answer(_mixed_areas_judge(score_claims=False), _answer(MIXED_AREAS), ws, "?", [],
                      requirements=_areas_turn())
    assert {o["id"]: o["status"] for o in r.requirement_outcomes()}["N1.2"] == "full"  # unit-level, as in round 7


def test_a_vote_naming_a_span_the_unit_does_not_hold_falls_back_to_the_unit():
    ws = _ws(AREAS_ONLY)
    judge = _mixed_areas_judge()
    r = verify_answer(judge, _answer(MIXED_AREAS), ws, "?", [], requirements=_areas_turn())
    r.requirement_votes["N1.2"][0].claims[:] = ["משפט שאינו ביחידה"]
    assert {o["id"]: o["status"] for o in r.requirement_outcomes()}["N1.2"] == "full"


def test_a_conditional_component_with_no_value_in_the_read_source_forces_nothing_and_is_not_repaired():
    ws = _ws(AREAS_ONLY)
    _opened(ws, "תא שטח 1")
    turn = _turn({"id": "1", "text": "השטחים העיקריים"}, {"id": "2", "text": "תאריך האישור", "conditional": True})
    a = _answer("השטחים העיקריים הם 1,200 מ״ר [S1].")
    score = _scores({"N1": {"status": "full", "units": [0]}, "N2": {"status": "missing", "related": ["S1"]}})
    r = verify_answer(_judge(score), a, ws, "?", [], requirements=turn)
    assert r.ok and not [p for p in r.problems if p.kind in ("requirement", "instruction")]  # nothing is demanded
    final, outcomes = _finish(ws, a, r, turn)
    missing = r.completeness["missing"]
    assert [(m["id"], m["conditional"]) for m in missing] == [("N2", True)]
    assert "1,200" in final.answer_markdown and "תאריך האישור" in final.answer_markdown  # stated, never invented
    assert not re.search(r"\d{1,2}\.\d{1,2}\.\d{2,4}", final.answer_markdown)


# --- a partly given component names the part that is missing (round 8 R20, R21, AE7) ---------------------------------

SERVICE_QUESTION = "מהם שטחי השירות של תא שטח 1 ושל תא שטח 2?"
PLOT2_SERVICE = "שטחי השירות בתא שטח 2 הם 300 מ״ר [S1]."
PLOT1_SERVICE = "שטחי השירות בתא שטח 1"


def _partial_turn(component_text: str = "שטחי שירות", *, conditional: bool = False) -> TurnRequirements:
    return _turn({"id": "1", "text": component_text, "conditional": conditional})


def _partial_score(missing: str, *, absent=(), related=("H1",)):
    """The judge scores every component ``partial`` (given by unit 0) and names ``missing`` as the part not given."""
    def score(frozen, units):
        return [_req("", "partial", [0], related, i, absent=absent) | {"missing": missing} for i, _ in frozen]
    return score


def _partial_case(missing: str, answer: str = PLOT2_SERVICE, *, turn: TurnRequirements | None = None, absent=()):
    ws = _ws("תא שטח 2: שטחי השירות הם 300 מ״ר.")
    ws.searches.append("שטחי שירות")
    turn = turn or _partial_turn()
    a = _answer(answer)
    r = verify_answer(_judge(_partial_score(missing, absent=absent)), a, ws, SERVICE_QUESTION, [], requirements=turn)
    final, outcomes = _finish(ws, a, r, turn)
    return ws, a, r, final, outcomes


def test_ae7_a_partial_component_with_a_missing_part_gets_one_gap_line_naming_it_with_its_reason():
    _, a, r, final, outcomes = _partial_case(PLOT1_SERVICE)
    (o,) = outcomes
    assert o["status"] == "partial" and o["missing_part"] == PLOT1_SERVICE and o["stated"] is True
    assert o["limitation"] == "not_located"
    lines = _gap(final, a).split("\n")
    assert lines == [f"**{PLOT1_SERVICE}** לא נמצא בחיפוש במסמכים שנבדקו."], lines  # the part and its reason, once
    assert [g["texts"] for g in r.gaps] == [[PLOT1_SERVICE]]
    missing = r.completeness["missing"]
    assert [(m["id"], m["status"], m["missing_part"]) for m in missing] == [("N1", "partial", PLOT1_SERVICE)]
    assert r.completeness["status"] == "partial" and final.status == "partial"
    assert [c["missing_part"] for c in coverage.public_components(outcomes)] == [PLOT1_SERVICE]


def test_a_missing_part_with_a_word_neither_the_request_nor_the_component_holds_is_dropped():
    _, a, r, final, outcomes = _partial_case("שטחי השירות והמרפסות בתא שטח 3")
    (o,) = outcomes
    assert o["status"] == "partial" and o["missing_part"] == "" and o["stated"] is False  # today's behaviour
    assert _gap(final, a) == "" and r.gaps == []
    assert [m["missing_part"] for m in r.completeness["missing"]] == [""]  # the component is still listed in completeness


def test_a_conditional_partial_component_states_no_missing_part():
    _, a, r, final, outcomes = _partial_case(PLOT1_SERVICE, turn=_partial_turn(conditional=True))
    (o,) = outcomes
    assert o["status"] == "partial" and o["stated"] is False and o["missing_part"] == ""
    assert _gap(final, a) == "" and r.gaps == []


def test_a_model_sentence_stating_the_missing_part_absent_is_replaced_by_the_one_gap_line():
    answer = PLOT2_SERVICE + "\n" + "שטחי השירות בתא שטח 1 לא נמצאו."
    _, a, r, final, outcomes = _partial_case(PLOT1_SERVICE, answer, absent=[1])
    line = f"**{PLOT1_SERVICE}** לא נמצא בחיפוש במסמכים שנבדקו."
    assert final.answer_markdown.count(PLOT1_SERVICE) == 1, final.answer_markdown  # one owner of the gap
    assert final.answer_markdown.split("\n\n")[-1] == line and "לא נמצאו" not in final.answer_markdown
    assert PLOT2_SERVICE in final.answer_markdown and outcomes[0]["status"] == "partial"


def test_the_missing_part_is_validated_word_by_word_against_the_component_and_the_request():
    ok = verify.valid_missing
    request = SERVICE_QUESTION
    assert ok(PLOT1_SERVICE, "partial", "שטחי שירות", request) == PLOT1_SERVICE  # prefixes ה ב are ignored
    assert ok("  שטחי   השירות\nבתא שטח 1 ", "partial", "שטחי שירות", request) == PLOT1_SERVICE
    assert ok("שטחי שירות של תא שטח 2", "partial", "שטחי שירות", request) == "שטחי שירות של תא שטח 2"  # stop word
    assert ok("שטחי שירות בתא שטח 3", "partial", "שטחי שירות", request) == ""  # 3 was never asked for
    assert ok("הארנונה", "partial", "שטחי שירות", request) == ""  # a word of neither text
    assert ok(PLOT1_SERVICE, "full", "שטחי שירות", request) == ""  # only a partial requirement has a missing part
    assert ok("", "partial", "שטחי שירות", request) == ""
    assert ok("של את", "partial", "שטחי שירות", request) == ""  # stop words only: nothing is named
    assert ok(PLOT1_SERVICE + " " + "x" * 200, "partial", "שטחי שירות", request) == ""  # not a short noun phrase


def test_the_judge_policy_and_schema_carry_the_missing_part():
    assert "missing" in verify.JudgeRequirement.model_fields
    assert verify.JudgeRequirement(status="partial").missing == ""
    assert "missing" in verify.JUDGE_REQUIREMENTS_POLICY and "partial" in verify.JUDGE_REQUIREMENTS_POLICY
    assert "missing_part" in coverage.PUBLIC_FIELDS


# --- U16 (R18, R24; AE8): a gap names a short noun phrase and its true cause --------------------------------------------

UNIT_TITLES = {1: ("שומה א", 1, 4), 2: ("שומה ב", 5, 9)}


def _in_units(ws: Workspace, *, located: tuple[int, ...] = (), read_in: int | None = None) -> None:
    """S1's file holds two appraisal units (``tools.unit_info``); the turn worked inside ``located`` through a unit
    handle (``unit_scopes``); S1 lies in unit ``read_in``."""
    from app.chat.contexts import Contexts, Identity, Segment
    from app.chat.tools import unit_info

    source = ws.sources["S1"]
    cx = Contexts(str(source.version_id), tuple(
        Segment(n, Identity(), n * 10, n * 10 + 9, n * 10, None, first, last, "appraisal", "certain", title)
        for n, (title, first, last) in UNIT_TITLES.items()), "r1")
    ws.contexts[str(source.version_id)] = cx
    for n in located:
        ws.unit_scopes[cx.key(n)] = {"document_id": str(source.document_id), "version_id": str(source.version_id),
                                     "reading_id": "r1", "unit_no": n}
    if read_in is not None:
        source.contexts, source.unit = [read_in], unit_info(cx, read_in)


def _stopped_on(ws: Workspace, *limits: str) -> None:
    ws.limits_hit.extend(limits)


def _only_missing(ws: Workspace, related: list[str] | None = None, *, turn: TurnRequirements | None = None):
    final, outcomes, gap = _missing_with(ws, turn or TurnRequirements(), related if related is not None else ["H1"])
    return outcomes[1], gap


def test_ae8_a_turn_that_ran_out_of_time_before_locating_the_unit_says_not_located_within_the_time_limit():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    _in_units(ws)
    _stopped_on(ws, "time_limit")
    o, gap = _only_missing(ws)
    assert (o["limitation"], o["variant"]) == ("limit_reached", "time_limit")
    assert gap == "**דמי הניהול** לא אותר במסגרת מגבלת הזמן של השאלה."
    assert "לא נמצא" not in gap and "לא מופיע" not in gap  # R18: never "not in the document"


def test_a_turn_that_spent_its_reading_budget_or_steps_says_so_not_that_the_datum_is_absent():
    for limit, sentence in (("tool_budget", "במסגרת מגבלת היקף הקריאה של השאלה"),
                            ("step_limit", "במסגרת מספר הצעדים המרבי של השאלה")):
        ws = _ws()
        ws.searches.append("דמי ניהול")
        _stopped_on(ws, limit)
        o, gap = _only_missing(ws)
        assert o["limitation"] == "limit_reached" and o["variant"] == limit, (limit, o["limitation"])
        assert gap == f"**דמי הניהול** לא אותר {sentence}.", gap


def test_a_complete_read_inside_the_unit_beats_the_limit_and_a_partial_or_no_read_does_not():
    # a section read to its end in a one-unit file, then the time ran out: the limit did not cause the absence
    whole = _ws()
    _opened(whole)
    _stopped_on(whole, "time_limit")
    o, gap = _only_missing(whole, ["S1"])
    assert o["limitation"] == "not_in_part_read" and gap == "**דמי הניהול** לא מופיע בסעיף \"תיאור הנכס\" שנבדק [S1]."
    # the same inside the located unit of a file of several
    inside = _ws()
    _opened(inside)
    _in_units(inside, located=(1,), read_in=1)
    _stopped_on(inside, "time_limit")
    assert _only_missing(inside, ["S1"])[0]["limitation"] == "not_in_part_read"
    # a complete read of another unit than the located one proves nothing about it
    other = _ws()
    _opened(other)
    _in_units(other, located=(2,), read_in=1)
    _stopped_on(other, "time_limit")
    assert _only_missing(other, ["S1"])[0]["limitation"] == "limit_reached"
    # a partial read, and no read at all
    part = _ws()
    _opened(part, complete=False)
    _stopped_on(part, "time_limit")
    o, gap = _only_missing(part, ["S1"])
    assert o["limitation"] == "limit_reached" and "במסגרת מגבלת הזמן" in gap and "לא מופיע" not in gap
    none = _ws()
    none.searches.append("דמי ניהול")
    _stopped_on(none, "time_limit")
    assert _only_missing(none)[0]["limitation"] == "limit_reached"


def test_the_time_limit_line_says_which_unit_stays_in_focus_when_one_was_located():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    _in_units(ws, located=(2,))
    _stopped_on(ws, "time_limit")
    o, gap = _only_missing(ws)
    assert o["limitation"] == "limit_reached"
    assert gap == ("**דמי הניהול** לא אותר במסגרת מגבלת הזמן של השאלה. «שומה ב» (עמודים 5–9) נשאר בפוקוס; "
                   "אפשר לבקש להמשיך לקרוא.")


def test_a_file_that_is_one_unit_gets_the_limit_line_without_a_unit_suffix():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    _stopped_on(ws, "time_limit")
    _, gap = _only_missing(ws)
    assert gap == "**דמי הניהול** לא אותר במסגרת מגבלת הזמן של השאלה."


def test_a_file_of_several_units_with_none_located_gives_unit_not_located_never_not_found():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    _in_units(ws)
    o, gap = _only_missing(ws)
    assert o["limitation"] == "unit_not_located" and o["limitation_text"] == coverage.REASONS["unit_not_located"]
    assert gap == "**דמי הניהול**: השומה (היחידה בקובץ) שהשאלה מתייחסת אליה לא אותרה, ולכן לא נבדק אם הנתון מופיע בה."
    assert "לא נמצא" not in gap and "לא מופיע" not in gap


def test_a_professional_question_whose_pool_was_empty_gets_the_standard_absent_gap_not_a_search_that_found_nothing():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    ws.intent, ws.standards_absent = "professional", True
    o, gap = _only_missing(ws)
    assert o["limitation"] == "standard_absent" and o["limitation_text"] == "נוסח התקן אינו במאגר"
    assert gap == "**דמי הניהול**: נוסח התקן אינו במאגר, ולכן לא נבדק מה התקן עצמו קובע בעניין הזה."
    assert "לא אותר" not in gap and "לא נמצא" not in gap
    # a gap of the repository, stated before a limit of the turn: it is why nothing was read in a standard
    _stopped_on(ws, "time_limit")
    assert _only_missing(ws)[0]["limitation"] == "standard_absent"
    # another question type, or a pool that was not empty, says what it always said
    for intent, absent in (("property", True), ("professional", False)):
        other = _ws()
        other.searches.append("דמי ניהול")
        other.intent, other.standards_absent = intent, absent
        assert _only_missing(other)[0]["limitation"] != "standard_absent"


def test_not_in_part_read_needs_the_read_section_to_lie_inside_the_located_unit():
    inside = _ws()
    _opened(inside)
    _in_units(inside, located=(1,), read_in=1)
    o, gap = _only_missing(inside, ["S1"])
    assert o["limitation"] == "not_in_part_read" and gap == "**דמי הניהול** לא מופיע בסעיף \"תיאור הנכס\" שנבדק [S1]."
    outside = _ws()
    _opened(outside)
    _in_units(outside, located=(2,), read_in=1)  # the unit the turn is in is another one than the section read
    o, gap = _only_missing(outside, ["S1"])
    assert o["limitation"] != "not_in_part_read" and "לא מופיע" not in gap, gap
    assert o["limitation"] == "not_located" and "אף חיפוש או קריאה לא כיסו" in gap


def test_a_complete_read_of_a_one_unit_file_is_still_not_present_in_that_section():
    ws = _ws()
    _opened(ws)
    o, _ = _only_missing(ws, ["S1"])
    assert o["limitation"] == "not_in_part_read"


def test_the_new_reasons_come_before_not_in_part_read_and_not_located_in_the_gap_paragraph():
    keys = list(coverage.REASONS)
    assert keys.index("region_not_read") < keys.index("limit_reached") < keys.index("unit_not_located") \
        < keys.index("not_in_part_read") < keys.index("not_located")


def test_a_component_that_is_the_whole_question_gets_its_subject_as_the_gap_label():
    question = "מהו מספר היתר הבנייה של הבניין ומתי ניתן, ומהו תאריך הנפקת טופס 4 שלו?"
    ws = _ws()
    ws.searches.append("היתר")
    turn = _turn({"id": "1", "text": question, "subject": "פרויקט הדגמה"})
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    p = _judge(_scores({"N1": {"status": "missing", "related": ["H1"]}}))
    r = verify_answer(p, a, ws, question, [], requirements=turn)
    final, _ = _finish(ws, a, r, turn)
    gap = _gap(final, a)
    assert gap == "**הנתון המבוקש (פרויקט הדגמה)** לא נמצא בחיפוש במסמכים שנבדקו.", gap
    assert "?" not in gap and "מהו" not in gap and "ומתי" not in gap


def test_a_single_question_component_is_named_by_its_noun_phrase_and_a_clause_without_subject_by_a_generic_label():
    label = coverage.gap_label
    assert label({"text": "מהו מספר היתר הבנייה?", "subject": ""}) == "מספר היתר הבנייה"
    assert label({"text": "מספר היתר הבנייה", "subject": "בניין"}) == "מספר היתר הבנייה"
    assert label({"text": "מהו מספר ההיתר ומתי ניתן?", "subject": ""}) == "הנתון המבוקש"
    assert label({"text": "כמה עולה, והאם כולל מע\"מ?", "subject": "הנכס"}) == "הנתון המבוקש (הנכס)"
    # a short component the user wrote keeps its wording, a yes/no "האם …" one included (R24 is about long questions)
    assert label({"text": "האם הפרויקט כדאי", "subject": ""}) == "האם הפרויקט כדאי"
    assert label({"text": "האם הנכס מושכר", "status": "partial", "missing_part": "", "subject": "נכס"}) == "האם הנכס מושכר"
    # a long or multi-clause yes/no question gets the subject label
    assert label({"text": "האם הפרויקט כדאי כלכלית ומתי כדאי להתחיל לבנות", "subject": "פרויקט"}) == "הנתון המבוקש (פרויקט)"
    assert label({"text": "האם הפרויקט כדאי כלכלית לאור כל העלויות והתמורות שנקבעו בדוח השמאי של הנכס", "subject": "הנכס"}) \
        == "הנתון המבוקש (הנכס)"
    assert label({"text": "האם הפרויקט כדאי?", "subject": ""}) == "הנתון המבוקש"
    assert label({"text": "שטחי שירות", "status": "partial", "missing_part": PLOT1_SERVICE}) == PLOT1_SERVICE


def test_a_missing_part_that_is_a_request_clause_is_rejected():
    ok = verify.valid_missing
    request = "מהו מספר היתר הבנייה ומתי ניתן? ומהו תאריך הנפקת טופס 4?"
    component = "מספר היתר הבנייה ומתי ניתן"
    assert ok("מספר היתר הבנייה", "partial", component, request) == "מספר היתר הבנייה"
    assert ok("מהו מספר היתר הבנייה", "partial", component, request) == ""  # a question word: a clause
    assert ok("מספר היתר הבנייה ומתי ניתן", "partial", component, request) == ""
    assert ok("תאריך הנפקת טופס 4?", "partial", component, request) == ""  # ends with a question mark
    assert ok("האם ניתן היתר", "partial", component, request) == ""
    assert ok("תאריך הנפקת טופס 4", "partial", component, request) == "תאריך הנפקת טופס 4"


def test_an_unlinked_model_claim_whose_label_is_a_clause_gets_the_generic_label():
    ws = _ws()
    ws.searches.append("היתר")
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].", requested=[Requested(
        label="מהו מספר היתר הבנייה ומתי ניתן?", status="not_found_search", document_ids=[], component="",
        checked_where="")])
    items = coverage._unlinked_gaps(ws, a.requested)
    assert [i["text"] for i in items] == ["הנתון המבוקש"]


# --- pairwise conflicts (round 12 U11: R18, AE7; KTD13) -----------------------------------------------------------------------------

def _when(iso: str | None, precision: str = "day", end: str | None = None, origin: str | None = None) -> dict:
    out = {"date": iso, "precision": precision if iso else None, "end": end, "provenance": "source" if iso else None,
           "forecast": False, "locator": None}
    return out | ({"date_origin": origin} if origin else {})


def _item(amount: str, when: dict | None, key: str = "k"):
    return coverage.Comparable(key, Decimal(amount), when)


def test_pairs_two_dated_periods_conflict_only_when_they_overlap():
    items = [_item("100", _when("2023-06-01")), _item("120", _when("2023-06-01")), _item("130", _when("2024-06-01")),
             _item("140", _when("2023", "year")), _item("150", _when("2022-01-01", end="2023-07-01"))]
    # 2 (2024) overlaps none of the others; 4 is a range from 2022 into July 2023, so it overlaps the 2023 ones
    assert coverage.conflicting_pairs(items) == [(0, 1), (0, 3), (0, 4), (1, 3), (1, 4), (3, 4)]


def test_pairs_an_undated_member_conflicts_unless_it_is_history_or_a_comparable():
    dated = _item("100", _when("2025-03-31"))
    assert coverage.conflicting_pairs([dated, _item("90", None)]) == [(0, 1)]
    assert coverage.conflicting_pairs([dated, _item("90", _when(None))]) == [(0, 1)]
    assert coverage.conflicting_pairs([dated, _item("90", _when(None, origin="history"))]) == []
    assert coverage.conflicting_pairs([dated, _item("90", _when(None, origin="comparable"))]) == []
    assert coverage.conflicting_pairs([_item("90", None), _item("80", None)]) == [(0, 1)]
    assert coverage.conflicting_pairs([_item("90", None), _item("80", _when(None, origin="history"))]) == []


def test_pairs_the_same_amount_or_another_key_is_no_conflict():
    assert coverage.conflicting_pairs([_item("100", None), _item("100", _when("2025-01-01"))]) == []
    assert coverage.conflicting_pairs([_item("100", None, "a"), _item("90", _when("2025-01-01"), "b")]) == []


def test_pairs_each_pair_is_decided_alone():
    items = [_item("100", _when("2023-06-01")), _item("120", _when("2024-06-01")), _item("100", None)]
    assert coverage.conflicting_pairs(items) == [(1, 2)]


def _dated_value(ws: Workspace, written: str, when: dict | None, **kw) -> str:
    vid = _value(ws, written, "השווי", "value", **({"grounded": True} | kw))
    ws.values[vid].as_of = when
    return vid


def test_conflicting_reads_a_dated_and_an_undated_value_as_one_conflict_unless_the_undated_is_history():
    ws = _ws()
    doc = uuid.uuid4()  # round 13 U4: one identity (the timeline's), a grounded kind, unit and VAT
    _dated_value(ws, "2,450,000", _when("2025-03-31"), document=doc)
    undated = _dated_value(ws, "2,300,000", None, document=doc)
    assert coverage.conflicting(ws)
    ws.values[undated].as_of = _when(None, origin="history")
    assert not coverage.conflicting(ws)
    ws.values[undated].as_of = _when("2024-03-31")
    assert not coverage.conflicting(ws)
    ws.values[undated].as_of = _when("2025", "year")
    assert coverage.conflicting(ws)
