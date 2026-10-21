"""Cheaper repair rounds (U11, KTD10, R31): a repair round re-judges only the units that changed, problems the
server resolves itself do not cost a repair round, and a judge's ``partial`` costs one only when it names a concrete
defect. Requirements derived once per turn are still scored by id across fresh and cached verdicts (KTD7).

The judge and the model are scripted: these tests prove which units reach the judge and what the server does with
its verdicts, not the model's judgement. Synthetic texts and amounts only; no database."""

from __future__ import annotations

import re
import threading
import uuid

import pytest

from app.chat import engine, verify
from app.chat import request as request_analysis
from app.chat import tools as T
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import TurnRequirements, VerdictCache, verify_answer
from app.config import get_settings
from app.db import TenantContext
from app.providers.llm import CallStatus, Purpose
from tests.support.scripted_agent import ScriptedAgent, call, component, final
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
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
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
                r'<requirement id="(N[\d.]+)"[^>]*>\n(.*?)\n</requirement>', input, re.S)
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
        ("N1", "full"), ("N2", "full"), ("N3", "not_answered")]
    moved = "הנתונים לקוחים מהדוח [S1].\n" + REPAIRED  # every unit's index moves by one
    again = verify_answer(p, _answer(moved), ws, "?", [], requirements=turn, cache=cache)
    assert list(_units(seen[1])) == [0, 3]  # the new opening line and the repaired sentence
    assert '<requirement id="N1"' in seen[1] and "<derive_requirements>" not in seen[1]
    outcomes = again.requirement_outcomes()
    assert [(o["id"], o["status"], o["units"]) for o in outcomes] == [
        ("N1", "full", [1]), ("N2", "full", [2]), ("N3", "full", [3])]
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
    assert [(o["id"], o["status"]) for o in again.requirement_outcomes()] == [("N1", "full")]


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


def test_a_simple_first_turn_takes_its_agent_steps_one_request_analysis_and_one_verify_call(offline):
    agent = ScriptedAgent([SEARCH, final(REPAIRED)])
    out = _turn(agent)
    # no resolve call on a first turn; the request analysis (KTD1, round 7) ran beside the first step and is
    # collected before the second
    assert [u["purpose"] for u in out.usage] == ["agent", "request", "agent", "verify"]
    assert out.answer.status == "answered" and out.report.ok
    s = out.summary
    assert s["calls"] == 4 and s["by_purpose"] == {"agent": 2, "request": 1, "verify": 1}
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


def _judge_wrong_management(input):
    return {"verdicts": [{"index": i, "verdict": _wrong_management(t), "reason": "בדיקה"}
                         for i, t in _units(input).items()]}


@pytest.mark.parametrize("failure", [CallStatus.TIMEOUT, CallStatus.ERROR])
def test_a_repair_round_that_fails_keeps_the_verified_answer_without_its_unsupported_sentence(offline, failure):
    agent = ScriptedAgent([SEARCH, final(THREE), failure], judge=_judge_wrong_management)
    out = _turn(agent)
    assert len(agent.seen) == 3 and len(_verify_inputs(agent)) == 1
    assert out.answer.answer_markdown.strip() and out.report.counts()["removed"] >= 1
    assert out.summary["rounds"] == 1  # the turn ended with the verified answer, not a ProviderFailure


def test_a_repair_round_that_would_not_fit_in_the_time_left_is_not_started(offline, monkeypatch):
    monkeypatch.setattr(engine, "REPAIR_TIME_FACTOR", 1e12)  # any answer step "took" longer than the turn has left
    agent = ScriptedAgent([SEARCH, final(THREE), final(REPAIRED)], judge=_judge_wrong_management)
    out = _turn(agent)
    assert len(agent.seen) == 2 and len(_verify_inputs(agent)) == 1
    assert out.report.counts()["removed"] >= 1


def test_an_answer_whose_only_problem_is_a_missing_qualifier_gets_no_repair_round(offline):
    offline.text = QUALIFIED
    agent = ScriptedAgent([SEARCH, final("השווי למ\"ר בצאלון 7 הוא 14,250 ₪ [S1]."), final("לא אמור לקרות.")])
    out = _turn(agent, "מה השווי למ\"ר בצאלון 7?")
    assert len(agent.seen) == 2 and len(_verify_inputs(agent)) == 1
    assert "14,250 ₪ (מ״ר אקוו׳, כפי שנכתב במקור) [S1]" in out.answer.answer_markdown
    assert out.report.counts()["annotated"] == 1 and out.report.counts()["scoped"] == 0


@pytest.mark.parametrize("defect, steps", [("none", 2), ("formula", 4)])  # 4: a repair, then a rewrite
def test_a_partial_verdict_starts_a_repair_round_only_with_a_concrete_defect(offline, defect, steps):
    md = "השווי למ\"ר הוא 9,500 ₪ [S1]."
    agent = ScriptedAgent([SEARCH, final(md), final(md), final(md)], judge=lambda input: {"verdicts": [
        {"index": i, "verdict": "partial", "reason": "בדיקה", "defect": defect} for i in _units(input)]})
    out = _turn(agent)
    assert len(agent.seen) == steps
    assert "אומת חלקית" in out.answer.answer_markdown


AREA_CLAUSE = "השטח העיקרי בתא שטח 1 הוא 1,200 מ״ר"
UNAPPROVED = "שאושר בוועדה המחוזית"


def _mixed_area_judge(input: str) -> dict:
    """The area is supported by the source and the approval is not: the unit is mixed (judge rule 16)."""
    return {"verdicts": [{"index": i, "verdict": "partial", "reason": "חלקי", "defect": "part", "claims": [
        {"span": AREA_CLAUSE, "verdict": "supported"}, {"span": UNAPPROVED, "verdict": "unsupported"}]}
        if UNAPPROVED in t else {"index": i, "verdict": "supported", "reason": "בדיקה"} for i, t in _units(input).items()]}


def test_ae1_a_mixed_sentence_finishes_with_one_agent_call_one_verify_call_and_no_repair_round(offline):
    offline.text = "תא שטח 1: השטח העיקרי הוא 1,200 מ״ר."
    sentence = f"{AREA_CLAUSE}, {UNAPPROVED} [S1]."
    agent = ScriptedAgent([SEARCH, final(sentence), final("לא אמור לקרות.")], judge=_mixed_area_judge)
    out = _turn(agent, "מה השטח העיקרי בתא שטח 1?")
    # the failing clause is the server's to cut: no rewrite round, and one judge call more — the sentence as it is saved,
    # with the cut in it, is a changed model-written unit, checked once (round 8 U6, ``verify_final``)
    assert [u["purpose"] for u in out.usage] == ["agent", "request", "agent", "verify", "verify_final"]
    assert out.summary["rounds"] == 1 and len(_verify_inputs(agent)) == 2 and out.report.ok
    marker = "«[1: חלק שלא אומת הושמט]»"
    assert out.answer.answer_markdown == f"{AREA_CLAUSE}, {marker} [S1]." and out.answer.status == "partial"
    # the registry travels with the outcome, indexing the saved text
    assert [(s.kind, out.answer.answer_markdown[s.start:s.end]) for s in out.server_spans] == [("marker", marker)]
    assert [(c.text, c.action) for c in out.report.claims[0]] == [(AREA_CLAUSE, "kept"), (UNAPPROVED, "cut")]


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


# --- the request analysis before the answer (round 7 U2, KTD1, R1-R3) ---------------------------------------------

ANALYSED = [component("השווי למ\"ר"), component("דמי השכירות"), component("מראה מקום לכל נתון", "instruction")]


def _scored_by_id(seen: list | None = None):
    """A judge supporting every unit that scores each frozen requirement ``full`` by a unit naming its text, else
    ``missing`` (no unit, nothing related); it fails the test if asked to derive."""
    def respond(input: str) -> dict:
        if seen is not None:
            seen.append(input)
        assert "<derive_requirements>" not in input
        units = _units(input)
        frozen = re.findall(r'<requirement id="(N[\d.]+)"[^>]*>\n(.*?)\n</requirement>', input, re.S)
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units],
                "requirements": [_req("", "full" if hits else "missing", hits, (), rid) for rid, text in frozen
                                 for hits in [[i for i, t in units.items() if text.split()[0] in t]]]}
    return respond


def test_the_analysis_runs_beside_the_first_step_and_its_item_is_appended_before_the_second(offline):
    started = threading.Event()
    agent = ScriptedAgent([SEARCH, final(REPAIRED)], request=lambda input: started.set() or {
        "components": ANALYSED})
    during: list[bool] = []
    # the first step waits (briefly) for the analysis to start: it was started with the step, not after it
    agent.on_step = lambda i: during.append(started.wait(5)) if i == 0 else None
    out = _turn(agent)
    assert during == [True]
    first, second = agent.seen
    assert second[:len(first)] == first  # append-only: the cached prefix is unchanged
    added = [i for i in second[len(first):] if isinstance(i, dict) and i.get("role") == "user"]
    assert len(added) == 1 and all(f"N{n} " in added[0]["content"] for n in (1, 2, 3))
    assert [r["id"] for r in out.ledger["requirements"]] == ["N1", "N2", "N3"]
    assert [u["purpose"] for u in out.usage].count("request") == 1
    assert out.summary["requirements_origin"] == "analysis" and out.summary["requirements_fallback"] is None


def test_the_judge_scores_the_frozen_components_by_id_and_derives_nothing(offline):
    seen: list[str] = []
    agent = ScriptedAgent([SEARCH, final(REPAIRED)], judge=_scored_by_id(seen), request=ANALYSED)
    out = _turn(agent)
    assert seen and all('<requirement id="N3" kind="instruction">' in x for x in seen)
    assert [(r["id"], r["kind"]) for r in out.report.requirements] == [("N1", "information"), ("N2", "information"),
                                                                       ("N3", "instruction")]


def test_the_answers_parts_cannot_remove_or_narrow_a_frozen_component(offline):
    parts = [{"ask": "השווי למ\"ר", "answered": True, "missing_kind": "none"}]  # one part of three
    agent = ScriptedAgent([SEARCH, final(REPAIRED, parts=parts)], request=ANALYSED)
    out = _turn(agent)
    assert [r["id"] for r in out.report.requirements] == ["N1", "N2", "N3"]
    assert [r["text"] for r in out.report.requirements] == [c["text"] for c in ANALYSED]
    assert not any("<hint>" in c.input for c in agent.calls if c.purpose == Purpose.VERIFY)


def test_a_first_answer_at_the_first_step_still_waits_for_the_analysis_before_verification(offline):
    agent = ScriptedAgent([final(REPAIRED)], request=ANALYSED)
    out = _turn(agent)
    # nothing was searched, so a repair round follows; the analysis was collected before the first verification
    assert [u["purpose"] for u in out.usage][:3] == ["agent", "request", "verify"]
    assert [r["id"] for r in out.report.requirements] == ["N1", "N2", "N3"]


@pytest.mark.parametrize("failure", [CallStatus.TIMEOUT, CallStatus.ERROR])
def test_an_analysis_that_fails_falls_back_to_the_judges_derivation_and_says_so(offline, failure):
    seen: list[str] = []
    agent = ScriptedAgent([SEARCH, final(REPAIRED, parts=[{"ask": "השווי למ\"ר", "answered": True,
                                                         "missing_kind": "none"}])],
                          judge=lambda input: seen.append(input) or {
                              "verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"}
                                           for i in _units(input)],
                              "requirements": [_req("השווי למ\"ר", units=list(_units(input)))]},
                          request=failure)
    out = _turn(agent)
    assert out.answer.status == "answered" and "<derive_requirements>" in seen[0]
    assert [r["id"] for r in out.report.requirements] == ["N1"]
    assert out.summary["requirements_origin"] == "judge"
    assert out.summary["requirements_fallback"] == failure.value
    # the analysis item is never appended: the agent saw no component list
    assert not any("N1 " in str(i.get("content")) for i in agent.seen[1] if isinstance(i, dict)
                   and i.get("role") == "user")


def test_an_analysis_slower_than_its_bound_is_abandoned_and_the_turn_completes(offline, monkeypatch):
    monkeypatch.setattr(get_settings(), "llm_timeout_resolve_seconds", 1.2)
    monkeypatch.setattr(request_analysis, "ANALYSIS_GRACE_SECONDS", 0.0)
    gate = threading.Event()
    agent = ScriptedAgent([SEARCH, final(REPAIRED)], request=lambda input: gate.wait(10) and {"components": ANALYSED})
    out = _turn(agent)
    gate.set()
    assert out.answer.status == "answered"
    (entry,) = [u for u in out.usage if u["purpose"] == "request"]
    assert entry["status"] == "timeout"
    assert out.summary["requirements_origin"] == "judge" and out.summary["requirements_fallback"] == "timeout"


def test_an_instruction_component_is_never_sent_to_search(offline):
    turn = TurnRequirements()
    turn.adopt([{"id": "N1", "text": "דמי הניהול", "kind": "information"},
                {"id": "N2", "text": "כתיבה בסגנון מקצועי", "kind": "instruction"}], "analysis")
    p = _judge(score=lambda frozen, units, statements: [_req("", "missing", (), (), i) for i, _ in frozen])
    r = verify_answer(p, _answer(REPAIRED), _ws(), "?", [], requirements=turn)
    reasons = [x.reason for x in r.problems if x.kind == "requirement"]
    assert len(reasons) == 1 and "דמי הניהול" in reasons[0] and "לחפש" in reasons[0]
    assert not any("בסגנון" in x for x in reasons)


# --- a follow-up takes its components from the resolve call ------------------------------------------------------

def _follow_up(agent: ScriptedAgent, monkeypatch) -> engine.TurnOutcome:
    monkeypatch.setattr(engine.entities, "titles_of", lambda ctx: [])
    monkeypatch.setattr(engine.entities, "authorized", lambda ctx, ids: set(ids))
    inp = engine.TurnInput(question="ומה דמי השכירות?", history=[engine.HistoryMessage("user", "מה השווי?"),
                                                                 engine.HistoryMessage("assistant", "השווי הוא...")],
                           summary=None, focus_documents=[], prior_refs={})
    return engine.run_turn(_ctx(), agent, inp, lambda *a: None, lambda: False)


def _resolution(**kw) -> dict:
    return {"relation": "new_question", "scope": "entity", "standalone_question": "מה דמי השכירות בנכס?",
            "changed_fields": [], "metric_kind": "rent", "unit": "unknown", "scale": "unknown", "period": "unknown",
            "area_basis": "", "vat": "unknown", "subject": "", "document_ids": [], "ambiguity": ""} | kw


def test_a_follow_ups_components_come_from_resolve_with_no_extra_call(offline, monkeypatch):
    seen: list[str] = []
    agent = ScriptedAgent([SEARCH, final(REPAIRED)], judge=_scored_by_id(seen))
    agent.on(Purpose.RESOLVE, _resolution(components=[component("דמי השכירות"), component("השווי למ\"ר")]))
    out = _follow_up(agent, monkeypatch)
    assert [u["purpose"] for u in out.usage] == ["resolve", "agent", "agent", "verify"]  # no request analysis
    assert '<requirement id="N2"' in seen[0]
    assert out.summary["requirements_origin"] == "resolve"
    # the components are part of the turn's first message (built after the resolution), not a later item
    assert "N1 " in agent.seen[0][0]["content"] and len(agent.seen[1]) == len(agent.seen[0]) + 2


def test_a_follow_up_whose_component_part_is_invalid_uses_the_rest_and_the_judge_derives(offline, monkeypatch):
    seen: list[str] = []
    agent = ScriptedAgent([SEARCH, final(REPAIRED)], judge=lambda input: seen.append(input) or {
        "verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in _units(input)]})
    agent.on(Purpose.RESOLVE, _resolution(components=[{"id": "1", "text": "דמי השכירות", "kind": "wish"}]))
    out = _follow_up(agent, monkeypatch)
    assert out.request["standalone_question"] == "מה דמי השכירות בנכס?"  # the rest of the resolution is used
    assert "מה דמי השכירות בנכס?" in agent.seen[0][0]["content"]
    assert "<derive_requirements>" in seen[0]
    assert out.summary["requirements_origin"] == "judge" and out.summary["requirements_fallback"] == "invalid"
    assert [u["purpose"] for u in out.usage].count("request") == 0


# --- a repairable failure kind is sent to the repair round, and the decision records it (round 7 U4, KTD10) -------

def _wrong_subject_judge(input: str) -> dict:
    return {"verdicts": [{"index": i, "reason": "בדיקה"} | (
        {"verdict": "unsupported", "failure": "wrong_subject"} if "מושכר" in t else {"verdict": "supported"})
        for i, t in _units(input).items()]}


def _user_prompts(agent: ScriptedAgent) -> list[str]:
    return list(dict.fromkeys(i["content"] for step in agent.seen for i in step if isinstance(i, dict)
                              and i.get("role") == "user" and isinstance(i.get("content"), str)))


def test_a_repairable_kind_goes_to_the_repair_round_with_its_kind_and_ids_and_the_decision_records_the_attempt(
        offline):
    agent = ScriptedAgent([SEARCH, final(THREE), final(THREE), final(THREE)], judge=_wrong_subject_judge)
    out = _turn(agent)
    repairs = [p for p in _user_prompts(agent) if p.startswith("בדיקת האימות")]
    assert len(repairs) == 1  # one repair round with tools, within the existing budget
    assert verify.FAILURE_LABELS["wrong_subject"] in repairs[0] and "S1" in repairs[0]
    assert len(agent.seen) <= 2 + max(0, min(2, get_settings().chat_repair_rounds))
    (decision,) = out.report.removals()
    assert decision.failure_kind == "wrong_subject" and decision.repair_attempted is True
    assert "מושכר" not in out.answer.answer_markdown


def test_a_removal_with_no_repair_round_records_that_none_was_attempted(offline, monkeypatch):
    monkeypatch.setattr(engine, "REPAIR_TIME_FACTOR", 1e12)  # no repair round fits
    agent = ScriptedAgent([SEARCH, final(THREE)], judge=_wrong_subject_judge)
    out = _turn(agent)
    (decision,) = out.report.removals()
    assert decision.failure_kind == "wrong_subject" and decision.repair_attempted is False


# --- a repair round never drops a supported numeric claim (R8, R10, KTD4, KTD6) -----------------------------------------

LOST = ("דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\n"
        "הנכס פנוי [S1].")  # the repair fixes the failing sentence but drops the supported "9,500 ₪" one
NOTHING_ADDED = "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1]."  # the repair only drops the supported "9,500 ₪" sentence


def _entries(out, check: str) -> list[dict]:
    return [p for r in out.rounds for p in r if p["check"] == check]


def test_a_repair_that_drops_a_supported_numeric_sentence_and_adds_nothing_gives_way_to_the_checked_answer(offline):
    """Round 16 U3 (KTD4): the discard is the fallback when the repaired answer adds no grounded content."""
    agent = ScriptedAgent([SEARCH, final(THREE), final(NOTHING_ADDED)], judge=_judge_wrong_management)
    out = _turn(agent)
    md = out.answer.answer_markdown
    assert "9,500" in md and "55" in md  # the supported parts survive
    assert "מושכר" not in md  # the failing part is cut or removed, not rewritten in
    assert len(agent.seen) == 3  # no model call beyond the one repair round
    assert [u["purpose"] for u in out.usage].count("agent") == 3  # search step, answer, repair: nothing added
    assert len(_verify_inputs(agent)) == 1  # the repaired answer was never verified: the checked one is composed
    # recorded: a round of its own, checked "repair_lost_supported", one entry per lost claim
    lost = _entries(out, "repair_lost_supported")
    assert len(lost) == 1 and lost[0]["claim_id"] and "9,500" in lost[0]["text"]
    assert not _entries(out, "repair_restored_supported")
    assert out.report.ok is False and out.report.counts()["removed"] >= 1


def test_a_repair_that_adds_a_part_and_drops_a_supported_numeric_sentence_ends_with_both(offline):
    """AE3 (R5): the repair adds the part and rephrases away a verified number: the verified claim is put back."""
    agent = ScriptedAgent([SEARCH, final(THREE), final(LOST)], judge=_judge_wrong_management)
    out = _turn(agent)
    md = out.answer.answer_markdown
    assert "9,500" in md and "55" in md and "הנכס פנוי" in md  # the verified sentence and the addition
    assert "מושכר" not in md
    assert md.count("9,500") == 1  # restored once, as its own sentence with its citation
    assert "השווי למ\"ר הוא 9,500 ₪ [S1]" in md
    (entry,) = _entries(out, "repair_restored_supported")
    assert entry["claim_id"] and "9,500" in entry["text"]
    assert not _entries(out, "repair_lost_supported")
    # no model call beyond the ordinary ones: the merged answer's verification replaces the repaired answer's
    assert len(agent.seen) == 3 and [u["purpose"] for u in out.usage].count("agent") == 3
    assert len(_verify_inputs(agent)) == 2 and out.report.ok


def test_a_repair_that_keeps_the_supported_numbers_rephrased_is_adopted(offline):
    rephrased = ("ערך המ\"ר הוא 9500 ₪ [S1].\n"
                 "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\n"
                 "הנכס פנוי [S1].")
    agent = ScriptedAgent([SEARCH, final(THREE), final(rephrased)], judge=_judge_wrong_management)
    out = _turn(agent)
    assert out.answer.answer_markdown == rephrased
    assert not [p for r in out.rounds for p in r if p["check"] == "repair_lost_supported"]
    assert out.summary["rounds"] == 2 and out.report.ok


def test_a_supported_claim_without_numbers_dropped_by_a_repair_that_adds_a_part_is_restored(offline):
    """Round 9 U15 (R20) protects verified non-numeric information (a status, a date, a caveat, a plain fact) by its
    salient words. Round 16 U3 (KTD4, R5): when the repair that drops "הנכס פנוי" adds a grounded part, the sentence is put
    back beside it; when it adds nothing, the checked answer is composed instead (the next test)."""
    first = "הנכס פנוי [S1].\nהנכס מושכר [S1]."
    repaired = "דמי הניהול הם 12 ₪ למ\"ר [S1]."  # drops the supported sentence, which carries no number
    agent = ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management)
    out = _turn(agent)
    md = out.answer.answer_markdown
    assert "הנכס פנוי [S1]" in md and "12" in md and "מושכר" not in md
    (entry,) = _entries(out, "repair_restored_supported")
    assert entry["claim_id"] and "פנוי" in entry["text"]
    assert [u["purpose"] for u in out.usage].count("agent") == 3  # no model call beyond the one repair round


def test_a_supported_claim_without_numbers_gives_way_to_the_checked_answer_when_the_repair_adds_nothing_grounded(offline):
    first = "הנכס פנוי [S1].\nהנכס מושכר [S1]."
    repaired = "לא נמצא מידע על דמי תחזוקה."  # an absence statement, with no citation
    agent = ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management)
    out = _turn(agent)
    md = out.answer.answer_markdown
    assert "הנכס פנוי" in md and "מושכר" not in md and "תחזוקה" not in md
    (lost,) = _entries(out, "repair_lost_supported")
    assert lost["claim_id"] and "פנוי" in lost["text"]
    assert not _entries(out, "repair_restored_supported")
    assert [u["purpose"] for u in out.usage].count("agent") == 3 and len(_verify_inputs(agent)) == 1


def test_a_repair_that_rephrases_a_supported_claim_with_the_same_salient_words_is_adopted(offline):
    first = "הנכס פנוי [S1].\nהנכס מושכר [S1]."
    repaired = "הנכס עצמו פנוי [S1]."
    out = _turn(ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management))
    assert out.answer.answer_markdown == repaired
    assert not [p for r in out.rounds for p in r if p["check"] == "repair_lost_supported"]


def test_a_repair_that_drops_a_supported_date_only_claim_keeps_it_beside_its_addition(offline):
    """A date is verified information too: the month name is a qualifier word, so the repair must keep it. The
    repair's own addition (a status) stays as well."""
    first = "הנכס נבדק במרץ [S1].\nהנכס מושכר [S1]."
    repaired = "הנכס פנוי [S1]."
    out = _turn(ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management))
    md = out.answer.answer_markdown
    assert "הנכס נבדק במרץ [S1]" in md and "הנכס פנוי" in md and "מושכר" not in md
    assert [p["check"] for r in out.rounds for p in r].count("repair_restored_supported") == 1


def test_a_repair_that_rephrases_a_supported_caveat_with_the_same_salient_words_is_adopted(offline):
    offline.texts = [SOURCE + " הנכס פנוי בכפוף לאישור הוועדה."]
    first = "הנכס פנוי בכפוף לאישור הוועדה [S1].\nהנכס מושכר [S1]."
    repaired = "הנכס פנוי, וזאת בכפוף לאישור של הוועדה [S1]."
    out = _turn(ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management))
    assert out.answer.answer_markdown == repaired
    assert not [p for r in out.rounds for p in r if p["check"] == "repair_lost_supported"]


def test_a_repair_that_loses_the_caveat_of_a_supported_claim_gives_way_to_the_checked_answer(offline):
    """The sentence is still there with its facts, but the condition it was verified with is gone."""
    offline.texts = [SOURCE + " הנכס פנוי בכפוף לאישור הוועדה."]
    first = "הנכס פנוי בכפוף לאישור הוועדה [S1].\nהנכס מושכר [S1]."
    repaired = "הנכס פנוי [S1]."
    out = _turn(ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management))
    assert "בכפוף" in out.answer.answer_markdown
    assert [p["check"] for r in out.rounds for p in r].count("repair_lost_supported") == 1


def test_a_repair_that_turns_a_supported_negation_into_its_opposite_gives_way_to_the_checked_answer(offline):
    offline.texts = [SOURCE + " אין היתר בנייה."]
    first = "אין היתר בנייה [S1].\nהנכס מושכר [S1]."
    out = _turn(ScriptedAgent([SEARCH, final(first), final("יש היתר בנייה [S1].")], judge=_judge_wrong_management))
    assert "אין היתר" in out.answer.answer_markdown
    assert [p["check"] for r in out.rounds for p in r].count("repair_lost_supported") == 1


def test_a_repair_that_cites_none_of_the_sources_a_supported_sentence_cited_gives_way(offline):
    offline.texts = [SOURCE, OTHER]
    first = "הנכס פנוי [S1].\nהנכס מושכר [S1]."
    repaired = "הנכס פנוי [S2]."  # the same words, but its source is gone from the answer
    out = _turn(ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management))
    assert "[S1]" in out.answer.answer_markdown
    assert [p["check"] for r in out.rounds for p in r].count("repair_lost_supported") == 1


def test_a_repair_that_replaces_a_supported_absence_statement_with_the_value_is_adopted(offline):
    """An absence ("לא חושב כאן") is what a repair fills: its loss is no loss, though the sentence holds "לא"."""
    offline.texts = [SOURCE + " הסכום החדש לא חושב כאן."]
    first = "הסכום החדש לא חושב כאן [S1].\nהנכס מושכר [S1]."
    repaired = "השווי למ\"ר הוא 9,500 ₪ [S1]."
    out = _turn(ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management))
    assert out.answer.answer_markdown == repaired
    assert not [p for r in out.rounds for p in r if p["check"] == "repair_lost_supported"]


def test_a_repair_that_changed_a_computed_result_keeps_the_verified_one_beside_the_new_one(offline):
    """KTD4 / R11: a claim whose value the repair changed is put back beside the new one, so both bases stay visible."""
    offline.text = SOURCE + " סכום השורות בטבלה הוא 9,400 ₪."
    first = "הסכום הכולל הוא 9,500 ₪ לפי המסקנה [S1].\nהנכס מושכר [S1]."
    repaired = "הסכום הכולל הוא 9,400 ₪ לפי סכום השורות בטבלה [S1]."
    agent = ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management)
    out = _turn(agent)
    lines = out.answer.answer_markdown.split("\n")
    assert len(lines) == 2 and "9,400" in lines[0] and "9,500 ₪ לפי המסקנה [S1]" in lines[1]  # beside, right after
    assert len(_entries(out, "repair_restored_supported")) == 1 and not _entries(out, "repair_lost_supported")


def test_the_time_to_verify_the_merged_answer_is_missing_so_the_checked_answer_is_composed(offline):
    settings = get_settings()
    wanted = settings.chat_verify_min_seconds
    agent = ScriptedAgent([SEARCH, final(THREE), final(LOST)], judge=_judge_wrong_management)

    def squeeze(step: int) -> None:
        if step == 2:  # the repair round's answer arrives with less time than a verification needs
            settings.chat_verify_min_seconds = 10**6

    agent.on_step = squeeze
    try:
        out = _turn(agent)
    finally:
        settings.chat_verify_min_seconds = wanted
    md = out.answer.answer_markdown
    assert "9,500" in md and "55" in md and "מושכר" not in md and "הנכס פנוי" not in md  # the checked answer, composed
    assert len(_verify_inputs(agent)) == 1
    assert len(_entries(out, "repair_lost_supported")) == 1 and not _entries(out, "repair_restored_supported")


# --- the restore helper: which claims, which sentence, where (round 16 U3, KTD4) -----------------------------------

def _fixed(respond) -> ScriptedProvider:
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda instructions, input: respond(input), repeat=True)
    return p


def _mixed_judge(input: str) -> dict:
    """A unit that holds a verified number and a failing status is judged as two claims."""
    out = []
    for i, t in _units(input).items():
        if "מושכר" in t and "9,500" in t:
            out.append({"index": i, "verdict": "partial", "reason": "חלקי", "defect": "part", "claims": [
                {"span": "השווי למ\"ר הוא 9,500 ₪", "verdict": "supported"},
                {"span": "והנכס מושכר", "verdict": "unsupported", "failure": "absent_from_source"}]})
        else:
            out.append({"index": i, "verdict": _wrong_management(t), "reason": "בדיקה"})
    return {"verdicts": out}


def test_a_unit_with_one_verified_and_one_failing_claim_has_only_the_verified_sentence_restored():
    report = verify_answer(_fixed(_mixed_judge), _answer("השווי למ\"ר הוא 9,500 ₪ והנכס מושכר [S1]."), _ws(), "?", [])
    repaired = "דמי הניהול הם 12 ₪ למ\"ר [S1]."
    lost = report.lost_supported(repaired)
    assert [r.text for _, r in lost] == ["השווי למ\"ר הוא 9,500 ₪"]  # the failing claim is not protected
    merged, back = report.restore_lost(repaired, lost)
    assert len(back) == 1
    assert merged == repaired + "\n\nהשווי למ\"ר הוא 9,500 ₪ [S1]."
    assert "מושכר" not in merged


def test_a_restored_sentence_goes_after_the_unit_holding_its_neighbouring_claim_and_the_rest_stays():
    first = ("## שכירות\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\nהשווי למ\"ר הוא 9,500 ₪ [S1].\n"
             "## ניהול\nדמי הניהול הם 12 ₪ למ\"ר [S1].")
    report = verify_answer(_judge(), _answer(first), _ws(), "?", [])
    repaired = ("## שכירות\nהשווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי [S1].\n"
                "## ניהול\nדמי הניהול הם 12 ₪ למ\"ר [S1].")
    lost = report.lost_supported(repaired)
    assert [r.text.rstrip(" .") for _, r in lost] == ["דמי השכירות הם 55 ₪ למ\"ר לחודש"]
    merged, back = report.restore_lost(repaired, lost)
    assert merged == ("## שכירות\nהשווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\nהנכס פנוי [S1].\n"
                      "## ניהול\nדמי הניהול הם 12 ₪ למ\"ר [S1].")
    assert len(back) == 1


def test_a_claim_the_repair_kept_in_other_words_is_not_restored_again_and_the_rest_is():
    first = "הנכס פנוי [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1]."
    report = verify_answer(_judge(), _answer(first), _ws(), "?", [])
    repaired = "הנכס עצמו פנוי [S2].\nדמי הניהול הם 12 ₪ למ\"ר [S2]."  # re-cited: both are lost, one is said again
    assert len(report.lost_supported(repaired)) == 2
    merged, back = report.restore_lost(repaired, report.lost_supported(repaired))
    assert [r.text.rstrip(" .") for _, r in back] == ["דמי השכירות הם 55 ₪ למ\"ר לחודש"]
    assert merged.count("פנוי") == 1 and merged.count("55") == 1


def test_an_added_part_needs_a_citation_a_new_number_or_new_words():
    report = verify_answer(_judge(), _answer("הנכס פנוי בכפוף לאישור הוועדה [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1]."),
                           _ws(), "?", [])
    assert report.adds_content("דמי הניהול הם 12 ₪ למ\"ר [S1].")  # a number no verified claim holds
    assert report.adds_content("החצר משותפת לכל הדיירים [S1].")  # words no verified claim holds
    assert not report.adds_content("דמי השכירות הם 55 ₪ [S1].")  # a verified claim, shorter
    assert not report.adds_content("הנכס פנוי [S1].")  # a verified claim, without its caveat
    assert not report.adds_content("החצר משותפת לכל הדיירים.")  # no citation: nothing grounded
    assert not report.adds_content("לא נמצא מידע על דמי תחזוקה [S1].")  # an absence is no content


def test_a_negation_that_qualifies_a_positive_claim_is_protected_but_an_absence_statement_is_not():
    ws = _ws(SOURCE + " המחיר הוא 55 ₪ ואינו כולל מע״מ. הסכום לא חושב.")
    report = verify_answer(_judge(), _answer("המחיר הוא 55 ₪ ואינו כולל מע״מ [S1].\nהסכום לא חושב [S1]."), ws, "?", [])
    assert not report.lost_supported("המחיר הוא 55 ₪ ואינו כולל מע״מ [S1].")  # nothing lost: the absence is free
    (lost,) = report.lost_supported("המחיר הוא 55 ₪ [S1].")
    assert "מע״מ" in lost[1].text  # dropping "אינו כולל מע״מ" from a supported price claim is a loss
    assert not report.lost_supported("המחיר הוא 55 ₪ ואינו כולל מע״מ [S1].\nהסכום חושב והוא 3 ₪ [S1].")
    for text in ("הקומה אינה כלולה בשטח", "אין אפשרות לחשב את הסכום", "לא ניתן לקבוע את השווי", "לא נמצא שטח"):
        assert verify.states_absence(text) == (text != "הקומה אינה כלולה בשטח")


def test_a_repair_that_drops_a_threshold_a_period_or_a_vat_word_of_a_verified_claim_restores_the_earlier_sentence():
    # round 18 U5 (KTD1, repair guard): the lexicon holds the condition, threshold, period and VAT words, so a rewrite that keeps the
    # number and loses the word that limits it is a loss, and the verified sentence is put back (``restore_lost``)
    ws = _ws(SOURCE + " השיעור חל עד 30 שנה. דמי הניהול הם 12 ₪ למ\"ר לחודש. המחיר 55 ₪ כולל מע״מ. זכויות מעל 100 מ\"ר.")
    first = ("השיעור חל עד 30 שנה [S1].\nדמי הניהול הם 12 ₪ למ\"ר לחודש [S1].\nהמחיר הוא 55 ₪ כולל מע״מ [S1].\n"
             "הזכויות מעל 100 מ\"ר [S1].")
    report = verify_answer(_judge(), _answer(first), ws, "?", [])
    for dropped, word in (("השיעור חל 30 שנה [S1].", "עד"), ("דמי הניהול הם 12 ₪ למ\"ר [S1].", "לחודש"),
                          ("המחיר הוא 55 ₪ [S1].", "מע״מ"), ("הזכויות 100 מ\"ר [S1].", "מעל")):
        others = [ln for ln in first.split("\n") if word not in ln]
        repaired = "\n".join([dropped, *others])
        lost = report.lost_supported(repaired)
        assert [word in r.text for _, r in lost] == [True], word
        merged, back = report.restore_lost(repaired, lost)
        assert len(back) == 1 and word in merged, word
    keeps = "\n".join(first.split("\n"))
    assert not report.lost_supported(keeps)


def test_a_qualified_or_cited_supported_claim_is_protected_like_a_kept_one():
    ws = _ws()
    report = verify_answer(_judge(), _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי."), ws, "?", [])
    for recs in report.claims.values():
        for r in recs:
            r.action = "cited"
    assert report.lost_supported("הנכס מושכר [S1].") and not report.lost_supported(
        "השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי [S1].")
    for recs in report.claims.values():
        for r in recs:
            r.action = "qualified"
    assert len(report.lost_supported("הנכס מושכר [S1].")) == 2
    for recs in report.claims.values():
        for r in recs:
            r.action = "cut"  # a cut claim failed: its loss is no loss
    assert not report.lost_supported("הנכס מושכר [S1].")


# --- round 21 U9 (R10, R3, KTD5): a repair round keeps the numbers verification supported ------------------------------------------

PARTIAL_THREE = ("השווי למ\"ר הוא 9,500 ₪ [S1].\n"
                 "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\n"
                 "הנכס מושכר [S1].")  # the number is partly supported (its unit doubted), a status is unsupported
PARTIAL_LOST = ("דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\n"
                "הנכס פנוי [S1].")  # the repair drops the partly supported number and adds a grounded part


def _partial_judge(defect: str, verdict: str = "partial", number: str = "9,500", once: bool = False):
    """A judge that calls the sentence of ``number`` ``verdict`` with ``defect`` and the one about the status unsupported; ``once``: only in
    its first call (the sentence as repaired or restored is then supported)."""
    calls: list = []

    def respond(input: str) -> dict:
        calls.append(input)
        out = []
        for i, t in _units(input).items():
            if number in t and not (once and len(calls) > 1):
                out.append({"index": i, "verdict": verdict, "reason": "חלקי", "defect": defect})
            else:
                out.append({"index": i, "verdict": _wrong_management(t), "reason": "בדיקה"})
        return {"verdicts": out}
    return respond


def test_a_partial_verdict_on_one_value_among_several_is_a_mark_and_no_repair_request():
    """AE5 (R10): the number is in the source and the judge doubts only that it is one value among several the source gives: the sentence
    stays, marked as partly verified, and no repair round is spent on it."""
    report = verify_answer(_fixed(_partial_judge("multiple_values")), _answer("השווי למ\"ר הוא 9,500 ₪ [S1]."), _ws(), "?", [])
    (p,) = report.problems
    assert p.severity == "partial" and p.defect == "none" and not p.repairable and report.ok
    assert "אומת חלקית" in report.edit(_answer("השווי למ\"ר הוא 9,500 ₪ [S1].")).doc.text
    assert "השרת מסמן" in report.problems_text()  # the repair prompt does not ask to fix it


@pytest.mark.parametrize("defect", ["units", "part", "formula", "input"])
def test_a_partial_verdict_with_another_defect_is_still_a_repair_request(defect):
    """``units`` too (R2, R3): a unit, period or VAT stated wrongly is not shown under a mark; the repair restates it as the source writes it."""
    report = verify_answer(_fixed(_partial_judge(defect)), _answer("השווי למ\"ר הוא 9,500 ₪ [S1]."), _ws(), "?", [])
    (p,) = report.problems
    assert p.severity == "partial" and p.defect == defect and p.repairable and not report.ok


def test_a_partial_verdict_on_a_claim_that_is_a_calculation_is_still_a_repair_request():
    """The number of a calculation is not one a source supports: only a datum keeps its number on a partial."""
    def respond(input: str) -> dict:
        return {"verdicts": [{"index": i, "verdict": "partial", "reason": "חלקי", "defect": "units", "claims": [
            {"span": "9,500 ₪", "kind": "computed", "verdict": "partial"}]} for i in _units(input)]}

    report = verify_answer(_fixed(respond), _answer("השווי למ\"ר הוא 9,500 ₪ [S1]."), _ws(), "?", [])
    (p,) = report.problems
    assert p.severity == "partial" and p.defect == "units" and p.repairable


def test_a_unit_partial_for_units_whose_number_no_source_states_is_not_kept_as_a_mark():
    report = verify_answer(_fixed(_partial_judge("units", number="7,777")), _answer("השווי למ\"ר הוא 7,777 ₪ [S1]."), _ws(), "?", [])
    assert report.removed_units() == {0} and not report.ok  # the deterministic number check fails it, as before


def _protected(first: str, judge, repaired: str):
    report = verify_answer(_fixed(judge), _answer(first), _ws(), "?", [])
    return report, report.lost_supported(repaired)


def test_a_partly_verified_number_dropped_by_a_repair_is_lost_and_restored_with_its_sentence():
    first = "השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1]."
    report, lost = _protected(first, _partial_judge("units"), "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\nהנכס פנוי [S1].")
    assert [r.text.rstrip(" .") for _, r in lost] == ["השווי למ\"ר הוא 9,500 ₪"]
    merged, back = report.restore_lost("דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\nהנכס פנוי [S1].", lost)
    assert len(back) == 1 and "השווי למ\"ר הוא 9,500 ₪ [S1]." in merged
    assert not report.lost_supported(first)  # kept as it was: nothing lost
    assert not report.lost_supported("הערך למ\"ר הוא 9500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].")  # the number, rephrased


def test_a_partly_verified_number_the_repair_rewrote_without_the_part_the_judge_doubted_is_not_lost():
    """Only the number (and a VAT basis it was written with) is protected: the doubted part may be what the repair fixes."""
    first = "השווי למ\"ר הוא 9,500 ₪ לחודש [S1]."
    _, lost = _protected(first, _partial_judge("units"), "השווי למ\"ר הוא 9,500 ₪ [S1].")
    assert lost == []


def test_a_partly_verified_number_keeps_the_vat_basis_the_source_gives_it():
    ws_text = "דוח: המחיר הוא 55 ₪ ללא מע״מ. הנכס פנוי."
    first = "המחיר הוא 55 ₪ [S1]."  # the judge doubts the unit; the source's own sentence states the basis
    report = verify_answer(_fixed(_partial_judge("units", number="55")), _answer(first), _ws(ws_text), "?", [])
    repaired = "המחיר הוא 55 ₪ [S1].\nהנכס פנוי [S1]."
    (lost,) = report.lost_supported(repaired)  # the number with no basis is a loss
    assert lost[1].source_sentence == "המחיר הוא 55 ₪ ללא מע״מ."
    merged, back = report.restore_lost(repaired, [lost])
    # the number is shown once: the basis is written at it, not a second sentence (R10)
    assert len(back) == 1 and merged == "המחיר הוא 55 ₪ ללא מע״מ [S1].\nהנכס פנוי [S1]." and merged.count("55") == 1
    assert not report.lost_supported("המחיר הוא 55 ₪ ללא מע״מ [S1].")


def test_a_number_dropped_by_the_repair_is_restored_with_the_sources_own_wording_not_the_doubted_one():
    ws_text = "דוח: המחיר הוא 55 ₪ לפני הנחות. הנכס פנוי."
    first = "המחיר הוא 55 ₪ בלבד [S1]."  # the judge doubts the sentence's wording of the price
    report = verify_answer(_fixed(_partial_judge("units", number="55")), _answer(first), _ws(ws_text), "?", [])
    merged, back = report.restore_lost("הנכס פנוי [S1].", report.lost_supported("הנכס פנוי [S1]."))
    assert len(back) == 1 and "המחיר הוא 55 ₪ לפני הנחות [S1]." in merged and "בלבד" not in merged


def test_a_number_the_source_states_twice_differently_is_not_protected_in_the_sources_name():
    ws_text = "דוח: המחיר הוא 55 ₪ לשנה. הוצע מחיר של 55 ₪ ללא מע״מ."
    report = verify_answer(_fixed(_partial_judge("units", number="55")), _answer("המחיר הוא 55 ₪ לחודש [S1]."), _ws(ws_text), "?", [])
    assert report.lost_supported("הנכס פנוי [S1].") == []


def test_a_basis_is_restored_as_its_own_sentence_when_the_number_has_no_unique_place():
    ws_text = "דוח: המחיר הוא 55 ₪ ללא מע״מ. הנכס פנוי."
    report = verify_answer(_fixed(_partial_judge("units", number="55")), _answer("המחיר הוא 55 ₪ [S1]."), _ws(ws_text), "?", [])
    repaired = "המחיר הוא 55 ₪ [S1].\nהמחיר בנכס אחר הוא 55 ₪ [S1]."  # the number stands in two units
    merged, back = report.restore_lost(repaired, report.lost_supported(repaired))
    assert len(back) == 1 and merged.count("ללא מע״מ") == 1 and all(ln in merged for ln in repaired.split("\n"))


@pytest.mark.parametrize("defect", ["part", "formula"])
def test_a_partial_with_another_defect_is_not_protected_because_its_number_may_be_the_doubted_part(defect):
    _, lost = _protected("השווי למ\"ר הוא 9,500 ₪ [S1].", _partial_judge(defect), "הנכס פנוי [S1].")
    assert lost == []


def test_a_contradicted_number_and_a_number_of_another_property_are_not_protected():
    first = "השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1]."
    _, lost = _protected(first, _partial_judge("units", verdict="contradicts"), "הנכס פנוי [S1].")
    assert [r.text.rstrip(" .") for _, r in lost] == ["דמי השכירות הם 55 ₪ למ\"ר לחודש"]  # only the supported one
    report = verify_answer(_fixed(_partial_judge("units")), _answer(first), _ws(), "?", [])
    for recs in report.claims.values():  # the attribution check decided the number belongs to another property
        for r in recs:
            if "9,500" in r.text:
                report.problems.append(verify.Problem(report.units[r.unit], "נתון של נכס אחר", failure_kind="wrong_subject",
                                                      check="judge", claim_id=r.id))
    assert [r.text.rstrip(" .") for _, r in report.lost_supported("הנכס פנוי [S1].")] == ["דמי השכירות הם 55 ₪ למ\"ר לחודש"]


def test_a_repair_that_drops_a_partly_verified_number_and_adds_a_part_ends_with_the_number_marked(offline):
    """The engine: the repair round (asked for by the unsupported status) omits the number the judge called partly supported."""
    rewritten = "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\nהשווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי [S1]."
    # the restored sentence is the one the judge doubted (its text is the source's own), so its cached verdict asks for a rewrite round
    agent = ScriptedAgent([SEARCH, final(PARTIAL_THREE), final(PARTIAL_LOST), final(rewritten)], judge=_partial_judge("units", once=True))
    out = _turn(agent)
    md = out.answer.answer_markdown
    line = next((ln for ln in md.splitlines() if "9,500" in ln), "")
    assert line.startswith("השווי למ\"ר הוא 9,500 ₪"), md  # the number stays, in the source's own wording
    assert md.count("9,500") == 1 and "הנכס פנוי" in md and "מושכר" not in md
    (entry,) = _entries(out, "repair_restored_supported")
    assert entry["claim_id"] and "9,500" in entry["text"]
    assert [u["purpose"] for u in out.usage].count("agent") == 4  # search, answer, repair, and the rewrite the cached partial verdict asks for


def test_a_repair_that_drops_a_partly_verified_number_and_adds_nothing_gives_way_to_the_checked_answer(offline):
    agent = ScriptedAgent([SEARCH, final(PARTIAL_THREE), final("דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].")],
                          judge=_partial_judge("units"))
    out = _turn(agent)
    md = out.answer.answer_markdown
    assert "9,500" in md and "אומת חלקית" in md and "מושכר" not in md
    (lost,) = _entries(out, "repair_lost_supported")
    assert "9,500" in lost["text"]


def test_a_partial_on_a_supported_number_alone_spends_no_repair_round(offline):
    agent = ScriptedAgent([SEARCH, final("השווי למ\"ר הוא 9,500 ₪ [S1]."), final("לא אמור לקרות.")],
                          judge=_partial_judge("multiple_values"))
    out = _turn(agent)
    assert "9,500" in out.answer.answer_markdown and "אומת חלקית" in out.answer.answer_markdown
    assert len(agent.seen) == 2 and len(_verify_inputs(agent)) == 1


def test_the_repair_and_rewrite_prompts_keep_verified_numbers_and_state_what_was_not_verified():
    for prompt in (engine.REPAIR, engine.REWRITE):
        assert "מספר שאומת" in prompt and "ציין מה לא אומת" in prompt
    assert "אם אי אפשר לתמוך בהן" not in engine.REWRITE and "השמט אותה" not in engine.REWRITE  # the old lines: omit what was not verified
    assert "בקשת השלמה" in engine.REWRITE  # the open requests the rewrite carries


@pytest.mark.parametrize("quote, asked, held", [
    ("שיעור היוון למשרדים 6.5%", "שיעורי ההיוון שנקבעו", True),  # a number
    ("הסיבה לשימוש בשיעור שוק היא שהוא משקף את התשואה של נכס חלופי", "הסיבה לשימוש בשיעור שוק", True),  # a clause beyond the asked words
    ("שיעור זה חל רק בתנאי שהנכס מושכר במלואו ביום הקובע", "התנאי לתחולת שיעור ההיוון", True),
    ("שיעורי ההיוון שנקבעו בשומה", "שיעורי ההיוון שנקבעו", False),  # a heading that repeats the asked part
    ("שיעורי היוון לפי שימוש", "שיעורי ההיוון", False),  # a label
])
def test_a_quote_holds_a_value_of_the_asked_kind_only_beyond_a_heading_that_repeats_the_request(quote, asked, held):
    assert verify._holds_value(quote, asked) is held


def _repair_prompts(agent, lead: str) -> list[str]:
    return [p for p in _user_prompts(agent) if p.startswith(lead)]


def test_an_open_text_completion_request_reaches_the_rewrite_round(reading):
    """Round 21 U9 (KTD5): when a claim still fails after the repair and a rewrite follows, the part the answer still omits (a passage
    the turn read states it) is asked for in the rewrite round too: a rewrite with no tools can add it from that passage."""
    base = _rates_judge()

    def judge(input: str) -> dict:
        out = base(input)
        for v in out["verdicts"]:
            if "מושכר" in _units(input)[v["index"]]:
                v["verdict"] = "unsupported"
        return out

    bad = THREE_RATES + "\nהנכס מושכר [S1]."
    agent = _Recording([SEARCH, final(bad), final(bad), final(ALL_RATES)], judge=judge, request=[component(RATES_ASK)])
    out = _turn(agent, "מה שיעורי ההיוון שנקבעו?")
    (rewrite,) = _repair_prompts(agent, "גם התשובה המתוקנת")
    assert RATES[3] in rewrite and "S1" in rewrite  # the omitted rate, with its source
    assert agent.choices[-1] == "none"  # the rewrite has no tools
    assert out.answer.answer_markdown == ALL_RATES and out.report.ok


def test_a_request_that_needs_a_search_is_not_carried_into_the_rewrite_round_that_has_no_tools():
    r = verify.VerifyReport(units=[])
    unit = verify.Unit(-1, "", "", [])
    r.problems = [verify.Problem(unit, verify.REQ_NOT_SEARCHED.format(text="דמי ניהול"), kind="requirement", component="N2"),
                  verify.Problem(unit, verify.REQ_TEXT_FOUND.format(text="שיעורי היוון", where="S1 «6.5%»"), kind="requirement",
                                 component="N1", check=verify.TEXT_FOUND_CHECK)]
    text = r.problems_text(claims_only=True)
    assert "שיעורי היוון" in text and "דמי ניהול" not in text
    assert "דמי ניהול" in r.problems_text()  # the repair round (with tools) still gets it


def test_a_completion_whose_quoted_passage_holds_no_value_keeps_the_repair_rounds_tools(reading):
    """Round 21 U9 (KTD5): the passage the judge quoted for the omitted part is a heading, not a value of the asked kind."""
    heading = "שיעורי ההיוון שנקבעו בשומה"

    def judge(input: str) -> dict:
        units = _units(input)
        ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
        given = [i for i, t in units.items() if "למחסנים" in t]
        scored = [_req("", "full", list(units), id=ids[0]) if given else
                  _req("", "partial", list(units), id=ids[0]) | {"missing": "שיעורי ההיוון",
                                                                 "support": [{"id": "S1", "quote": heading}]}]
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units],
                "requirements": scored} if ids else {
            "verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units]}

    agent = _Recording([SEARCH, final(THREE_RATES), final(ALL_RATES)], judge=judge, request=[component(RATES_ASK)])
    _turn(agent, "מה שיעורי ההיוון שנקבעו?")
    assert agent.choices[:3] == [None, None, None]  # the repair step may read and search
    (repair,) = _repair_prompts(agent, "בדיקת האימות")
    assert "אין צורך בחיפוש חדש" not in repair and heading in repair


# --- reuse by stable id and context (round 8 U3, R18, KTD2, KTD7) ------------------------------------------------------

def _salted(*texts: str) -> Workspace:
    ws = _ws(*texts)
    ws.claim_salt = "salt"
    return ws


def test_a_reused_verdict_keeps_the_claim_id_and_the_same_heading_is_not_judged_again():
    seen: list[str] = []
    cache, ws = VerdictCache(), _salted()
    judge = _judge(seen=seen)
    markdown = "## מצב מוצע\nהשווי למ\"ר הוא 9,500 ₪ [S1]."
    first = verify_answer(judge, _answer(markdown), ws, "?", [], cache=cache)
    calls = len(seen)
    again = verify_answer(judge, _answer(markdown), ws, "?", [], cache=cache)
    assert len(seen) == calls and again.reused == 2
    assert [c.id for c in again.claims[1]] == [c.id for c in first.claims[1]]


def test_the_same_sentence_under_another_heading_or_table_header_gets_a_new_verdict_and_a_new_claim_id():
    seen: list[str] = []
    cache, ws = VerdictCache(), _salted()
    judge = _judge(seen=seen)
    first = verify_answer(judge, _answer("## מצב מוצע\nהשווי למ\"ר הוא 9,500 ₪ [S1]."), ws, "?", [], cache=cache)
    n = len(seen)
    moved = verify_answer(judge, _answer("## מצב מאושר\nהשווי למ\"ר הוא 9,500 ₪ [S1]."), ws, "?", [], cache=cache)
    assert len(seen) == n + 1 and moved.claims[1][0].id != first.claims[1][0].id
    row = "| נכס | שווי |\n| --- | --- |\n| א | 9,500 |"
    verify_answer(judge, _answer(row), ws, "?", [], cache=cache)
    n = len(seen)
    verify_answer(judge, _answer(row.replace("שווי", "שכירות")), ws, "?", [], cache=cache)
    assert len(seen) == n + 1  # the row under other column headers is judged again


def test_a_verdict_reused_after_a_reorder_maps_to_the_same_claim_ids():
    cache, ws = VerdictCache(), _salted()
    judge = _judge(lambda t: "unsupported" if "מושכר" in t else "supported")
    one = verify_answer(judge, _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס מושכר [S1]."), ws, "?", [], cache=cache)
    two = verify_answer(judge, _answer("הנכס מושכר [S1].\nהשווי למ\"ר הוא 9,500 ₪ [S1]."), ws, "?", [], cache=cache)
    ids = lambda r: {c.text: (c.id, c.verdict) for cs in r.claims.values() for c in cs}  # noqa: E731
    assert ids(one) == ids(two)


# --- the final check's reuse key (round 8 U6, KTD7): the model-written text, the server's spans excluded ------------------

def _final(monkeypatch, markdown: str, rule,
           texts: tuple[str, ...] = ("תכסית 40% מותרת. השווי למ\"ר הוא 9,500 ₪.",)):
    """The draft's report, its composition and the final check's view of both: ``(final, units, keys of the draft)``."""
    from app.chat import compose as compose_module
    from app.chat import coverage
    from app.chat.final_check import _Final
    from app.chat.verify import TurnRequirements, verify_answer

    monkeypatch.setattr(coverage, "build", lambda ws, answer, question: ({}, answer))  # no lookups: no database
    ws = Workspace(ctx=None)
    for t in texts:
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                      location="סעיף \"סיכום\"", kind="context", text=t)
    from tests.unit.test_chat_compose import _answer, _judge

    answer = _answer(markdown)
    report = verify_answer(_judge(rule), answer, ws, "?", [])
    turn = TurnRequirements()
    final = _Final(None, ws, answer, report, turn, question="?", request=None, usage=[], deadline=None,
                   limit_sentences=None)
    comp = compose_module.compose(ws, answer, report, turn, question="?")
    units = final.split(comp)
    return final, units, {d.index: final.draft_key(d) for d in report.units}, report


def test_the_server_spans_in_a_sentence_do_not_change_its_key(monkeypatch):
    markdown = "תכסית 40% [S1].\nהשווי למ\"ר הוא 9,500 ₪.\nדמי הניהול הם 12 ₪ למ\"ר [S1]."

    def rule(t):  # a partial mark; a citation the server attaches; a sentence removed (a removal note follows)
        if "9,500" in t:
            return {"verdict": "supported", "supported_by": ["S1"]}
        return {"verdict": "partial", "defect": "none"} if "תכסית" in t else {"verdict": "unsupported",
                                                                                 "failure": "absent_from_source"}

    final, units, draft, report = _final(monkeypatch, markdown, rule)
    model = [u for u in units if u.model]
    assert [u.drafts for u in model] == [[0], [1]]
    assert [u.key for u in model] == [draft[0], draft[1]]  # the key of the draft sentence: its verdict is reused
    # the partial mark and the removal note are units of the server's alone; the citation it attached sits inside the
    # sentence it cites, shown to the judge as the server's and left out of the key
    assert [u.model for u in units] == [True, False, True, False]
    assert [u.unit.server for u in units] == [[], ["*(אומת חלקית)*"], [" [S1]"], [units[3].unit.server[0]]]


def test_a_span_cut_out_of_a_sentence_changes_its_key_and_the_others_keep_theirs(monkeypatch):
    cut = "השטח העיקרי הוא 1,200 מ״ר, שאושר בוועדה המחוזית [S1]."
    markdown = f"{cut}\nהשווי למ\"ר הוא 9,500 ₪ [S1]."

    def rule(t):
        if "שאושר" in t:
            return {"verdict": "partial", "defect": "part", "claims": [
                {"span": "השטח העיקרי הוא 1,200 מ״ר", "verdict": "supported"},
                {"span": "שאושר בוועדה המחוזית", "verdict": "unsupported"}]}
        return {"verdict": "supported"}

    final, units, draft, report = _final(monkeypatch, markdown, rule, ("השטח העיקרי הוא 1,200 מ״ר. השווי למ\"ר הוא 9,500 ₪.",))
    first, second = [u for u in units if u.model]
    assert first.drafts == [0] and first.key != draft[0]  # as saved it is another text: it is judged again
    assert second.drafts == [1] and second.key == draft[1]


def test_removing_a_heading_line_changes_the_key_of_the_sentence_below_it(monkeypatch):
    markdown = "## מצב מוצע\nתכסית 40% [S1]."

    def heading_goes(t):
        return {"verdict": "unsupported", "failure": "absent_from_source"} if "מצב מוצע" in t else {"verdict": "supported"}

    final, units, draft, report = _final(monkeypatch, markdown, heading_goes)
    (below,) = [u for u in units if u.model]
    assert below.drafts == [1] and below.key != draft[1]  # the same words under another heading are another claim
    assert below.unit.heading == "" and report.units[1].heading == "מצב מוצע"
    # the heading line staying: the sentence keeps its key
    kept, units2, draft2, _ = _final(monkeypatch, markdown, lambda t: {"verdict": "supported"})
    assert [u.key for u in units2 if u.model] == [draft2[0], draft2[1]]


_MEANING_CHANGES = [
    ("הנכס מושכר [S1].", "הנכס אינו מושכר [S1]."),  # a negation added
    ("הנכס אינו מושכר [S1].", "הנכס מושכר [S1]."),  # a negation removed
    ("הנכס פנוי [S1].", "הנכס פנוי בכפוף לאישור הוועדה [S1]."),  # a condition added
    ("דמי הניהול הם 12 ₪ למ\"ר, אם יאושרו [S1].", "דמי הניהול הם 12 ₪ למ\"ר [S1]."),  # a condition removed
    ("הנכס פנוי [S1].", "הנכס מוצע להיות פנוי [S1]."),  # a status word added
]


@pytest.mark.parametrize(("before", "after"), _MEANING_CHANGES)
def test_a_negation_or_condition_added_or_removed_in_an_otherwise_identical_claim_is_another_key(before, after):
    """Round 9 U15 (R21): the key is the normalised text of the sentence, so a change of its meaning is never a reused
    verdict — in the key and in the cache of a repair round."""
    ws, request = _ws(), "?"
    key = lambda md: verify.unit_key(verify.split_units(md)[0], ws, request, "", {})  # noqa: E731
    assert key(before) != key(after) and key(before) == key(before)
    cache, seen = VerdictCache(), []
    judge = _judge(seen=seen)
    verify_answer(judge, _answer(before), ws, request, [], cache=cache)
    again = verify_answer(judge, _answer(after), ws, request, [], cache=cache)
    assert len(seen) == 2 and again.reused == 0  # judged again, not reused


def test_a_sentence_that_moves_to_the_front_of_its_line_keeps_its_key(monkeypatch):
    """"- A. B." without A is "- B.": the list marker is markup, not part of what was judged."""
    markdown = "- הנכס מושכר. השווי למ\"ר הוא 9,500 ₪ [S1]."

    def rule(t):
        return {"verdict": "unsupported", "failure": "absent_from_source"} if "מושכר" in t else {"verdict": "supported"}

    final, units, draft, report = _final(monkeypatch, markdown, rule)
    (value,) = [u for u in units if u.model]
    assert value.drafts == [1] and value.key == draft[1], value.unit.text


# --- round 16 U1: the judge contract is version 11 and the verdict cache key moves with it --------------------------------------

def test_the_judge_version_is_14_and_the_verdict_cache_key_changes_with_it(monkeypatch):
    assert verify.JUDGE_VERSION == 21  # 11: round 16 U1 (the read passages, `support`); 12: every returned passage, items of a kind; 13: round 17 U5 (defect `scope`); 14: round 18 U3 (a multi-report source header names the unit first; a bold label over a statement is shown as the statement); 15: round 18 U4 (the marks of a table part's attribution, rule 32); 16: round 18 U5 (the condition outcome of an item in the requirements call, `conditions`); 17: round 18 U6 (the candidates of the requirements call, `candidates`); 18: round 19 U2 (the `<ownership>` fact line of a unit whose evidence's owner is unknown, rule 34); 19: round 19 U2 (that line also tells of a source of another context cited beside the asked one's); 20: round 20 U2 (rule 35: the claims of a table row that holds a defect are its cells); 21: round 20 U3 (the source header names a unit's other names; the ownership line carries the scope and same-property facts)
    ws = _ws()
    unit = verify.split_units("השווי למ\"ר הוא 9,500 ₪ [S1].")[0]
    now = verify.unit_key(unit, ws, "?", "", {})
    monkeypatch.setattr(verify, "JUDGE_VERSION", 13)
    assert verify.unit_key(unit, ws, "?", "", {}) != now


# --- round 16 U2: a completion from read text runs the repair round with tools off (KTD3), and the cache carries its support ----

RATES = ["שיעור היוון למשרדים 6.5%", "שיעור היוון למסחר 7.0%", "שיעור היוון לחניה 8.25%", "מחסנים: 9.1%"]
RATES_TEXT = "שיעורי ההיוון שנקבעו בשומה:\n" + "\n".join(RATES)
RATES_ASK = "שיעורי ההיוון שנקבעו"
THREE_RATES = "שיעור היוון למשרדים 6.5% [S1].\nשיעור היוון למסחר 7.0% [S1].\nשיעור היוון לחניה 8.25% [S1]."
ALL_RATES = THREE_RATES + "\nשיעור ההיוון למחסנים הוא 9.1% [S1]."


class _Recording(ScriptedAgent):
    """A scripted agent that keeps the ``tool_choice`` each step was asked with."""

    def __init__(self, *a, **kw) -> None:
        super().__init__(*a, **kw)
        self.choices: list = []

    def agent_step(self, instructions, items, tools, final_schema, **kw):
        self.choices.append(kw.get("tool_choice"))
        return super().agent_step(instructions, items, tools, final_schema, **kw)


@pytest.fixture
def reading(monkeypatch):
    """Like ``offline``, but the search is a section the turn opened: its passage lists four rates and counts as read."""
    calls: list = []

    def run_tool(ws, name, arguments):
        calls.append(name)
        src = ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="שיעורי היוון",
                            location="סעיף \"שיעורי היוון\"", kind="context", text=RATES_TEXT)
        doc = str(src.document_id)
        ws.activity[doc] = {"title": "דוח בדיקה", "level": "read", "read": True, "partial": False, "read_partial": False,
                            "read_complete": True,
                            "openings": [{"sid": src.sid, "scope": "section", "name": "שיעורי היוון",
                                          "target": ("section", doc, src.sid)}]}
        return f'<source id="{src.sid}">{src.text}</source>'

    run_tool.calls = calls
    monkeypatch.setattr(T, "run_tool", run_tool)
    monkeypatch.setattr(engine.coverage, "build", lambda ws, answer, question: ({}, answer))
    return run_tool


def _rates_judge(extra: list[tuple[str, str]] = ()):
    """A judge scoring the rates component ``full`` when a unit names the warehouse rate, else ``partial`` with the fourth
    rate's row as support; ``extra``: more (id-less) components, each scored missing with no support."""
    def judge(input: str) -> dict:
        units = _units(input)
        ids = re.findall(r'<requirement id="(N[\d.]+)"', input)
        given = [i for i, t in units.items() if "למחסנים" in t]
        scored = [_req("", "full", list(units), id=ids[0]) if given else
                  _req("", "partial", list(units), id=ids[0]) | {"missing": "שיעורי ההיוון",
                                                                 "support": [{"id": "S1", "quote": RATES[3]}]}]
        scored += [_req("", "missing", [], id=i) for i in ids[1:]]
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units],
                "requirements": scored} if ids else {
            "verdicts": [{"index": i, "verdict": "supported", "reason": "בדיקה"} for i in units]}
    return judge


def test_ae1_a_pure_text_completion_runs_the_repair_step_with_tools_off_and_adds_the_part(reading):
    agent = _Recording([SEARCH, final(THREE_RATES), final(ALL_RATES)], judge=_rates_judge(),
                       request=[component(RATES_ASK)])
    out = _turn(agent, "מה שיעורי ההיוון שנקבעו?")
    assert agent.choices == [None, None, "none"]  # the repair step has no tools
    assert reading.calls == ["search"]  # the only search was the first step's: none in the repair round
    repair = next(i["content"] for i in agent.seen[2] if isinstance(i, dict) and i.get("role") == "user"
                  and "בדיקת האימות" in str(i.get("content")))
    assert "S1" in repair and RATES[3] in repair and "חיפוש" in repair
    assert out.answer.answer_markdown == ALL_RATES and out.report.ok
    assert [(o["id"], o["status"]) for o in out.report.requirement_outcomes()] == [("N1", "full")]


def test_a_repair_round_with_a_text_completion_and_a_component_nobody_searched_keeps_its_tools(reading):
    judge = _rates_judge()
    agent = _Recording([SEARCH, final(THREE_RATES), final(ALL_RATES)], judge=judge,
                       request=[component(RATES_ASK), component("דמי ניהול")])
    _turn(agent, "מה שיעורי ההיוון שנקבעו ומה דמי הניהול?")
    assert agent.choices[:2] == [None, None] and agent.choices[2] is None  # mixed: the repair step may search


def test_a_completion_that_fails_after_the_repair_leaves_the_part_stated_as_read_and_not_used(reading):
    agent = _Recording([SEARCH, final(THREE_RATES), final(THREE_RATES), final(THREE_RATES), final(THREE_RATES)],
                       judge=_rates_judge(), request=[component(RATES_ASK)])
    out = _turn(agent, "מה שיעורי ההיוון שנקבעו?")
    (o,) = [o for o in out.report.requirement_outcomes() if o["id"] == "N1"]
    assert o["status"] == "partial" and o["support"] == [{"id": "S1", "quote": RATES[3]}]
    assert reading.calls == ["search"]


def _restored(first: str, repaired: str, provider=None) -> tuple[str, list]:
    report = verify_answer(provider or _judge(), _answer(first), _ws(), "?", [])
    return report.restore_lost(repaired, report.lost_supported(repaired))


def test_sentences_restored_after_a_table_row_anchor_are_paragraphs_after_a_blank_line_not_rows():
    """Round 16 review #1: GFM absorbs a line right after a table row as another row, so a sentence restored behind a table
    follows a blank line (several stay on consecutive lines after it)."""
    first = ("| פריט | ערך |\n|---|---|\n| השווי למ\"ר | 9,500 ₪ [S1] |\n| דמי השכירות לחודש | 55 ₪ למ\"ר [S1] |")
    repaired = ("| פריט | ערך |\n|---|---|\n| השווי למ\"ר | 9,000 ₪ [S1] |\n| דמי השכירות לחודש | 60 ₪ למ\"ר [S1] |")
    merged, back = _restored(first, repaired)
    assert len(back) == 2
    assert merged == (repaired + "\n\nהשווי למ\"ר · 9,500 ₪ [S1].\nדמי השכירות לחודש · 55 ₪ למ\"ר [S1].")


def test_a_partial_span_of_a_table_row_restored_after_its_table_follows_a_blank_line():
    first = "| נכס | השווי למ\"ר הוא 9,500 ₪ והנכס מושכר [S1] |"
    repaired = "| נכס | השווי למ\"ר הוא 9,000 ₪ [S1] |"
    merged, back = _restored(first, repaired, _fixed(_mixed_judge))
    assert len(back) == 1
    assert merged == repaired + "\n\nנכס: השווי למ\"ר הוא 9,500 ₪ [S1]."
    assert "מושכר" not in merged


def test_a_sentence_restored_after_a_bullet_anchor_carries_the_list_lead_and_loses_the_list_mark_once():
    first = "- דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\n- השווי למ\"ר הוא 9,500 ₪ [S1]."
    repaired = "- השווי למ\"ר הוא 9,500 ₪ [S1].\n- הנכס פנוי [S1]."
    merged, back = _restored(first, repaired)
    assert len(back) == 1
    assert merged == "- השווי למ\"ר הוא 9,500 ₪ [S1].\n- דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\n- הנכס פנוי [S1]."


# --- round 17 U1: a carried requirement score is admitted only while its units and its component's evidence are unchanged --------

def _scored_cache():
    """A cache holding one verification of THREE with the requirements scored (N1 given by the value line, N2 by the rent line)."""
    ws, cache, turn = _ws(), VerdictCache(), TurnRequirements()
    p = _judge(score=_by_words(["השווי למ\"ר", "דמי השכירות"]))
    answer = _answer(THREE)
    verify_answer(p, answer, ws, "?", [], requirements=turn, cache=cache, item_version=1)
    units = verify.split_units(THREE)
    return cache, units, dict(enumerate(cache.last.keys))


def test_reuse_carries_the_scores_of_unchanged_units_and_reports_the_ones_it_did_not_carry():
    cache, units, keys = _scored_cache()
    verdicts, carried, refused = cache.reuse(units, units, keys)
    assert sorted(verdicts) == [0, 1, 2] and sorted(v.id for v in carried) == ["N1", "N2"] and refused == []
    changed = dict(keys) | {1: "another-key"}  # the rent line changed: N2 (given by it) is scored again with the unit it gave with
    verdicts, carried, refused = cache.reuse(units, units, changed)
    assert [v.id for v in carried] == ["N1"] and [(v.id, why) for v, why in refused] == [("N2", verify.RESCORED)]


def test_reuse_refuses_a_score_the_evidence_test_refuses_and_returns_its_giving_units_to_the_fresh_ones():
    cache, units, keys = _scored_cache()
    gave = {i for v in cache.last.votes if v.id == "N2" for i in v.units}
    assert gave and gave != {0, 1, 2}
    verdicts, carried, refused = cache.reuse(
        units, units, keys, lambda v: verify.EVIDENCE_CHANGED if v.id == "N2" else None)
    assert [v.id for v in carried] == ["N1"] and [(v.id, why) for v, why in refused] == [("N2", verify.EVIDENCE_CHANGED)]
    assert not gave & set(verdicts) and set(verdicts) == {0, 1, 2} - gave  # judged fresh in this same round, with no cached verdict
    assert sorted(i for i in verdicts) != [0, 1, 2]


def test_a_carried_score_keeps_the_version_it_was_cast_on_through_every_round():
    ws, cache, turn = _ws(), VerdictCache(), TurnRequirements()
    p = _judge(score=_by_words(["השווי למ\"ר", "דמי השכירות"]))
    for version in (1, 2, 3, 4):  # the same answer four times (the second round scores the components just derived once more)
        report = verify_answer(p, _answer(THREE), ws, "?", [], requirements=turn, cache=cache, item_version=version)
    stamps = [v._stamp for votes in report.requirement_votes.values() for v in votes]
    assert {s["version"] for s in stamps} <= {1, 2} and {s["origin"] for s in stamps} == {"carried"}  # none is of version 3 or 4
    assert report.version == 4 and report.requirement_decisions["N1"]["rests_on"][0]["version"] == 1
