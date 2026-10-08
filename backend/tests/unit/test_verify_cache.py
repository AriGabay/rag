"""Cheaper repair rounds (U11, KTD10, R31): a repair round re-judges only the units that changed, problems the
server resolves itself do not cost a repair round, and a judge's ``partial`` costs one only when it names a concrete
defect. Requirements derived once per turn are still scored by id across fresh and cached verdicts (KTD7).

The judge and the model are scripted: these tests prove which units reach the judge and what the server does with
its verdicts, not the model's judgement. Synthetic texts and amounts only; no database."""

from __future__ import annotations

import re
import uuid

import pytest

from app.chat import engine, verify
from app.chat import tools as T
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import TurnRequirements, VerdictCache, verify_answer
from app.db import TenantContext
from app.providers.llm import Purpose
from tests.support.scripted_agent import ScriptedAgent, call, final
from tests.support.scripted_provider import ScriptedProvider

SOURCE = ("סיכום: השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש. דמי הניהול הם 12 ₪ למ\"ר. "
          "הנכס פנוי.")
OTHER = "נספח: שטח המגרש הוא 640 מ\"ר. הנכס בן 3 קומות."
QUALIFIED = "בפרק התחשיב: השווי למ\"ר אקוו' לנכס ברחוב הצאלון 7 נקבע ל-14,250 ₪, ללא מע\"מ."

THREE = ("השווי למ\"ר הוא 9,500 ₪ [S1].\n"
         "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\n"
         "הנכס מושכר [S1].")
REPAIRED = ("השווי למ\"ר הוא 9,500 ₪ [S1].\n"
            "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\n"
            "הנכס פנוי [S1].")


def _ws(*texts: str) -> Workspace:
    ws = Workspace(ctx=None)
    for t in texts or (SOURCE,):
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                      location="סעיף \"סיכום\"", kind="context", text=t)
    return ws


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def _units(input: str) -> dict[int, str]:
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def _judge(rule=lambda text: "supported", seen: list | None = None, score=None, defect: str | None = None,
           named: dict[str, list[str]] | None = None) -> ScriptedProvider:
    """A judge answering each unit by ``rule(text)``; ``score(frozen, units, statements)`` scores the requirements
    (``frozen`` is None on the deriving call, else [(id, text)]); ``defect``: what a ``partial`` names; ``named``:
    for a unit whose text holds a key, the shown sources it names as its support. Inputs go to ``seen``."""
    p = ScriptedProvider()

    def respond(instructions: str, input: str) -> dict:
        if seen is not None:
            seen.append(input)
        units = _units(input)
        verdicts = []
        for i, t in units.items():
            v = {"index": i, "verdict": rule(t), "reason": "בדיקה",
                 "supported_by": next((ids for k, ids in (named or {}).items() if k in t), [])}
            if defect is not None:
                v["defect"] = defect
            verdicts.append(v)
        out = {"verdicts": verdicts}
        if score is not None and ("<derive_requirements>" in input or "<requirements>" in input):
            statements = {int(i): t for i, t in re.findall(r'<statement index="(\d+)">\n(.*?)\n</statement>', input,
                                                             re.S)}
            frozen = None if "<derive_requirements>" in input else re.findall(
                r'<requirement id="(Q\d+)"[^>]*>\n(.*?)\n</requirement>', input, re.S)
            out["requirements"] = score(frozen, units, statements)
        return out

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _req(text: str = "", status: str = "full", units=(), related=(), id: str = "") -> dict:
    return {"id": id, "text": text, "calculation": False, "status": status, "units": list(units),
            "related": list(related), "reason": "בדיקה"}


def _by_words(asks: list[str]):
    """Each ask ``full`` with the call's units that name it, else ``missing``."""
    def score(frozen, units, statements):
        names = [t for _, t in frozen] if frozen is not None else asks
        out = []
        for n, ask in enumerate(names):
            hits = [i for i, t in units.items() if ask in t]
            out.append(_req(ask, "full" if hits else "missing", hits, (), frozen[n][0] if frozen else ""))
        return out
    return score


def _wrong_management(text: str) -> str:
    return "unsupported" if "מושכר" in text else "supported"


# --- a repair round re-judges only what changed -----------------------------------------------------------------

def test_a_repair_that_changes_one_sentence_re_judges_one_unit():
    ws, cache, seen = _ws(), VerdictCache(), []
    p = _judge(_wrong_management, seen)
    first = verify_answer(p, _answer(THREE), ws, "?", [], cache=cache)
    assert first.removed_units() == {2} and len(_units(seen[0])) == 3
    again = verify_answer(p, _answer(REPAIRED), ws, "?", [], cache=cache)
    assert len(seen) == 2 and list(_units(seen[1])) == [2]
    assert again.ok and not again.problems and again.reused == 2 and again.judged


def test_an_unchanged_unsupported_unit_stays_unsupported_without_a_new_call():
    ws, cache, seen = _ws(), VerdictCache(), []
    p = _judge(_wrong_management, seen)
    verify_answer(p, _answer(THREE), ws, "?", [], cache=cache)
    again = verify_answer(p, _answer(THREE), ws, "?", [], cache=cache)
    assert len(seen) == 1  # nothing changed: no judge call at all
    assert again.removed_units() == {2} and "מושכר" not in again.apply(_answer(THREE)).answer_markdown


def test_a_unit_whose_cited_evidence_changed_is_re_judged():
    ws, cache, seen = _ws(SOURCE, OTHER), VerdictCache(), []
    p = _judge(seen=seen)
    verify_answer(p, _answer("הנכס פנוי [S1].\nשטח המגרש הוא 640 מ\"ר [S2]."), ws, "?", [], cache=cache)
    verify_answer(p, _answer("הנכס פנוי [S2].\nשטח המגרש הוא 640 מ\"ר [S2]."), ws, "?", [], cache=cache)
    assert list(_units(seen[1])) == [0]


def test_a_changed_table_header_re_judges_the_rows_under_it():
    ws, cache, seen = _ws(), VerdictCache(), []
    p = _judge(seen=seen)
    rows = "|---|---|\n| שווי למ\"ר | 9,500 ₪ [S1] |\n| דמי שכירות | 55 ₪ [S1] |"
    verify_answer(p, _answer("הנתונים:\n\n| נתון | סכום |\n" + rows), ws, "?", [], cache=cache)
    verify_answer(p, _answer("הנתונים:\n\n| נתון | סכום לחודש |\n" + rows), ws, "?", [], cache=cache)
    judged = list(_units(seen[1]).values())
    assert len(judged) == 3 and any("סכום לחודש" in t for t in judged)


def test_a_changed_heading_re_judges_the_lines_under_it():
    ws, cache, seen = _ws(), VerdictCache(), []
    p = _judge(seen=seen)
    body = "\n- השווי למ\"ר הוא 9,500 ₪ [S1].\n- הנכס פנוי [S1]."
    verify_answer(p, _answer("## נכס ברחוב הגפן" + body), ws, "?", [], cache=cache)
    verify_answer(p, _answer("## נכס ברחוב התאנה" + body), ws, "?", [], cache=cache)
    assert len(_units(seen[1])) == 3


def test_a_verdict_that_named_an_uncited_support_is_not_reused():
    ws, cache, seen = _ws(), VerdictCache(), []
    p = _judge(seen=seen, named={"פנוי": ["S1"]})
    md = "השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי."
    first = verify_answer(p, _answer(md), ws, "?", [], cache=cache)
    assert first.ok and "[S1]" in first.apply(_answer(md)).answer_markdown.split("\n")[1]
    verify_answer(p, _answer(md), ws, "?", [], cache=cache)
    assert list(_units(seen[1])) == [1]  # the support it named depended on what else the call showed


def test_without_a_cache_every_unit_is_judged_again():
    ws, seen = _ws(), []
    p = _judge(seen=seen)
    verify_answer(p, _answer(REPAIRED), ws, "?", [])
    verify_answer(p, _answer(REPAIRED), ws, "?", [])
    assert [len(_units(s)) for s in seen] == [3, 3]


# --- requirements are scored by id across fresh and cached verdicts (KTD7) --------------------------------------

def test_requirements_given_by_cached_units_survive_a_re_judge_that_moves_their_indexes():
    ws, cache, seen, turn = _ws(), VerdictCache(), [], TurnRequirements()
    p = _judge(_wrong_management, seen, score=_by_words(["השווי למ\"ר", "דמי השכירות", "פנוי"]))
    first = verify_answer(p, _answer(THREE), ws, "?", [], requirements=turn, cache=cache)
    assert [(o["id"], o["status"]) for o in first.requirement_outcomes()] == [
        ("Q1", "full"), ("Q2", "full"), ("Q3", "missing")]
    moved = "הנתונים לקוחים מהדוח [S1].\n" + REPAIRED  # every unit's index moves by one
    again = verify_answer(p, _answer(moved), ws, "?", [], requirements=turn, cache=cache)
    assert list(_units(seen[1])) == [0, 3]  # the new opening line and the repaired sentence
    assert '<requirement id="Q1"' in seen[1] and "<derive_requirements>" not in seen[1]
    outcomes = again.requirement_outcomes()
    assert [(o["id"], o["status"], o["units"]) for o in outcomes] == [
        ("Q1", "full", [1]), ("Q2", "full", [2]), ("Q3", "full", [3])]
    assert again.ok


def test_a_requirement_given_jointly_re_judges_its_unchanged_unit_with_the_changed_one():
    ws, cache, seen, turn = _ws(), VerdictCache(), [], TurnRequirements()

    def score(frozen, units, statements):  # one requirement, given only by the two lines together
        both = [i for i, t in units.items() if "השווי" in t or "השכירות" in t]
        status = "full" if len(both) == 2 else "partial" if both else "missing"
        return [_req("השווי ודמי השכירות", status, both, (), frozen[0][0] if frozen else "")]

    p = _judge(seen=seen, score=score)
    verify_answer(p, _answer(REPAIRED), ws, "?", [], requirements=turn, cache=cache)
    changed = REPAIRED.replace("55 ₪ למ\"ר לחודש", "55 ₪ למ\"ר לחודש, לפי הסקר")
    again = verify_answer(p, _answer(changed), ws, "?", [], requirements=turn, cache=cache)
    assert list(_units(seen[1])) == [0, 1]
    assert [o["status"] for o in again.requirement_outcomes()] == ["full"]


def test_an_identical_answer_over_a_changed_workspace_is_scored_again_without_its_units():
    ws, cache, seen, turn = _ws(), VerdictCache(), [], TurnRequirements()
    p = _judge(seen=seen, score=_by_words(["השווי למ\"ר"]))
    verify_answer(p, _answer(REPAIRED), ws, "?", [], requirements=turn, cache=cache)
    ws.searches.append("דמי ניהול")  # the repair round searched: the requirements are scored again
    again = verify_answer(p, _answer(REPAIRED), ws, "?", [], requirements=turn, cache=cache)
    assert len(seen) == 2 and not _units(seen[1]) and "H1" in seen[1]
    assert [(o["id"], o["status"]) for o in again.requirement_outcomes()] == [("Q1", "full")]


# --- what does not cost a repair round ----------------------------------------------------------------------------

def test_an_annotated_missing_qualifier_alone_does_not_fail_the_report_and_is_written_in():
    ws = _ws(QUALIFIED)
    a = _answer("השווי למ\"ר בצאלון 7 הוא 14,250 ₪ [S1].")
    r = verify_answer(_judge(), a, ws, "?", [])
    assert r.ok and any(p.annotatable for p in r.problems)
    assert "14,250 ₪ (מ״ר אקוו׳, כפי שנכתב במקור) [S1]" in r.apply(a).answer_markdown


def test_a_missing_qualifier_the_server_cannot_write_still_fails_the_report():
    ws = _ws("שווי הדירה ברחוב הערבה 2 הוא 12,600 ₪ למ\"ר פלדלת.\n"
             "בטבלת ההשוואה, הדירה ברחוב הערבה 2 נמכרה ב-12,600 ₪ למ\"ר ברוטו.")
    r = verify_answer(_judge(), _answer("בערבה 2 הערך הוא 12,600 ₪ למ\"ר [S1]."), ws, "?", [])
    qualifiers = [p for p in r.problems if p.kind == "missing_qualifier"]
    assert qualifiers and not any(p.annotatable for p in qualifiers) and not r.ok


def test_an_attached_citation_alone_does_not_fail_the_report():
    ws = _ws()
    md = "השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי."
    r = verify_answer(_judge(named={"פנוי": ["S1"]}), _answer(md), ws, "?", [])
    assert r.ok and [p.kind for p in r.problems] == ["needs_citation"]


@pytest.mark.parametrize("defect, ok", [("none", True), ("formula", False), ("input", False), ("part", False)])
def test_a_partial_verdict_costs_a_repair_only_when_it_names_a_concrete_defect(defect, ok):
    ws = _ws()
    a = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].")
    r = verify_answer(_judge(lambda t: "partial", defect=defect), a, ws, "?", [])
    assert r.ok is ok
    assert r.correctness() == "partial" and "אומת חלקית" in r.apply(a).answer_markdown  # marked either way


def test_the_judge_schema_and_policy_name_the_defect_and_an_older_reply_stays_valid():
    assert "defect" in verify.JudgeVerdict.model_fields and "defect" in verify.JUDGE_POLICY
    assert verify.JudgeVerdict(index=0, verdict="partial", reason="x").defect == "none"


# --- the engine's repair loop -----------------------------------------------------------------------------------

def _ctx() -> TenantContext:
    return TenantContext(office_id=uuid.uuid4(), user_id=uuid.uuid4(), role="admin")


@pytest.fixture
def offline(monkeypatch):
    """The turn without a database: the search tool registers the synthetic passage, and the coverage ledger (which
    looks titles up) is empty."""
    def run_tool(ws, name, arguments):
        src = ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                            location="סעיף \"סיכום\"", kind="context", text=run_tool.text)
        return f'<source id="{src.sid}">{src.text}</source>'

    run_tool.text = SOURCE
    monkeypatch.setattr(T, "run_tool", run_tool)
    monkeypatch.setattr(engine.coverage, "build", lambda ws, answer, question: ({}, answer))
    return run_tool


def _turn(agent: ScriptedAgent, question: str = "מה השווי ודמי השכירות?") -> engine.TurnOutcome:
    inp = engine.TurnInput(question=question, history=[], summary=None, focus_documents=[], prior_refs={})
    return engine.run_turn(_ctx(), agent, inp, lambda *a: None, lambda: False)


def _verify_inputs(agent: ScriptedAgent) -> list[str]:
    return [c.input for c in agent.calls if c.purpose == Purpose.VERIFY]


SEARCH = [call("search", query="שווי", document_ids=None, limit=None)]


def test_a_simple_first_turn_takes_its_agent_steps_and_one_verify_call(offline):
    agent = ScriptedAgent([SEARCH, final(REPAIRED)])
    out = _turn(agent)
    assert [u["purpose"] for u in out.usage] == ["agent", "agent", "verify"]  # no resolve call on a first turn
    assert out.answer.status == "answered" and out.report.ok
    s = out.summary
    assert s["calls"] == 3 and s["by_purpose"] == {"agent": 2, "verify": 1}
    assert s["input_tokens"] == 20 and s["output_tokens"] == 20 and s["cached_input_tokens"] == 0
    assert set(s) >= {"cost_usd", "latency_ms", "verdicts_reused", "rounds"} and s["rounds"] == 1


def test_the_engines_repair_round_re_judges_only_the_changed_sentence(offline):
    agent = ScriptedAgent([SEARCH, final(THREE), final(REPAIRED)], judge=lambda input: {"verdicts": [
        {"index": i, "verdict": _wrong_management(t), "reason": "בדיקה"} for i, t in _units(input).items()]})
    out = _turn(agent)
    judged = [_units(i) for i in _verify_inputs(agent)]
    assert [len(j) for j in judged] == [3, 1] and list(judged[1]) == [2]
    assert out.answer.answer_markdown == REPAIRED and out.answer.status == "answered"
    assert out.summary["verdicts_reused"] == 2 and out.summary["rounds"] == 2


def test_an_answer_whose_only_problem_is_a_missing_qualifier_gets_no_repair_round(offline):
    offline.text = QUALIFIED
    agent = ScriptedAgent([SEARCH, final("השווי למ\"ר בצאלון 7 הוא 14,250 ₪ [S1]."), final("לא אמור לקרות.")])
    out = _turn(agent, "מה השווי למ\"ר בצאלון 7?")
    assert len(agent.seen) == 2 and len(_verify_inputs(agent)) == 1
    assert "14,250 ₪ (מ״ר אקוו׳, כפי שנכתב במקור) [S1]" in out.answer.answer_markdown
    assert out.report.counts()["annotated"] == 1


@pytest.mark.parametrize("defect, steps", [("none", 2), ("formula", 4)])  # 4: a repair, then a rewrite
def test_a_partial_verdict_starts_a_repair_round_only_with_a_concrete_defect(offline, defect, steps):
    md = "השווי למ\"ר הוא 9,500 ₪ [S1]."
    agent = ScriptedAgent([SEARCH, final(md), final(md), final(md)], judge=lambda input: {"verdicts": [
        {"index": i, "verdict": "partial", "reason": "בדיקה", "defect": defect} for i in _units(input)]})
    out = _turn(agent)
    assert len(agent.seen) == steps
    assert "אומת חלקית" in out.answer.answer_markdown


def test_usage_summary_adds_up_the_calls_of_a_turn():
    usage = [{"purpose": "agent", "input_tokens": 100, "cached_input_tokens": 80, "cache_write_tokens": None,
              "output_tokens": 10, "latency_ms": 900, "cost_usd": 0.001},
             {"purpose": "verify", "input_tokens": 50, "cached_input_tokens": None, "cache_write_tokens": 5,
              "output_tokens": 20, "latency_ms": 400, "cost_usd": None}]
    s = engine.usage_summary(usage)
    assert s["calls"] == 2 and s["by_purpose"] == {"agent": 1, "verify": 1}
    assert (s["input_tokens"], s["cached_input_tokens"], s["cache_write_tokens"], s["output_tokens"]) == (150, 80, 5,
                                                                                                          30)
    assert s["cost_usd"] == pytest.approx(0.001) and s["unpriced_calls"] == 1 and s["latency_ms"] == 1300
