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
    assert out.report.counts()["annotated"] == 1


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


def test_a_repair_that_drops_a_supported_numeric_sentence_gives_way_to_the_checked_answer(offline):
    agent = ScriptedAgent([SEARCH, final(THREE), final(LOST)], judge=_judge_wrong_management)
    out = _turn(agent)
    md = out.answer.answer_markdown
    assert "9,500" in md and "55" in md  # the supported parts survive
    assert "מושכר" not in md and "הנכס פנוי" not in md  # the failing part is cut or removed, not rewritten in
    assert len(agent.seen) == 3  # no model call beyond the one repair round
    assert [u["purpose"] for u in out.usage].count("agent") == 3  # search step, answer, repair: nothing added
    # recorded: a round of its own, checked "repair_lost_supported", one entry per lost claim
    lost = [p for r in out.rounds for p in r if p["check"] == "repair_lost_supported"]
    assert len(lost) == 1 and lost[0]["claim_id"] and "9,500" in lost[0]["text"]
    assert out.report.ok is False and out.report.counts()["removed"] >= 1


def test_a_repair_that_keeps_the_supported_numbers_rephrased_is_adopted(offline):
    rephrased = ("ערך המ\"ר הוא 9500 ₪ [S1].\n"
                 "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\n"
                 "הנכס פנוי [S1].")
    agent = ScriptedAgent([SEARCH, final(THREE), final(rephrased)], judge=_judge_wrong_management)
    out = _turn(agent)
    assert out.answer.answer_markdown == rephrased
    assert not [p for r in out.rounds for p in r if p["check"] == "repair_lost_supported"]
    assert out.summary["rounds"] == 2 and out.report.ok


def test_a_supported_claim_without_numbers_gives_way_to_the_checked_answer_when_the_repair_drops_it(offline):
    """Round 9 U15 (R20). This test used to pin the opposite — ``..._does_not_hold_back_a_repair``: a supported sentence
    with no number could be dropped by a repair round, because only numbers were protected. Verified non-numeric
    information (a status, a date, a caveat, a plain fact) is now protected by its salient words: the repair that drops
    "הנכס פנוי" is not adopted, and the checked answer — with its failing sentence removed — is composed instead."""
    first = "הנכס פנוי [S1].\nהנכס מושכר [S1]."
    repaired = "השווי למ\"ר הוא 9,500 ₪ [S1]."  # drops the supported sentence, which carries no number
    agent = ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management)
    out = _turn(agent)
    md = out.answer.answer_markdown
    assert "הנכס פנוי" in md and "מושכר" not in md and "9,500" not in md
    (lost,) = [p for r in out.rounds for p in r if p["check"] == "repair_lost_supported"]
    assert lost["claim_id"] and "פנוי" in lost["text"]
    assert [u["purpose"] for u in out.usage].count("agent") == 3  # no model call beyond the one repair round


def test_a_repair_that_rephrases_a_supported_claim_with_the_same_salient_words_is_adopted(offline):
    first = "הנכס פנוי [S1].\nהנכס מושכר [S1]."
    repaired = "הנכס עצמו פנוי [S1]."
    out = _turn(ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management))
    assert out.answer.answer_markdown == repaired
    assert not [p for r in out.rounds for p in r if p["check"] == "repair_lost_supported"]


def test_a_repair_that_drops_a_supported_date_only_claim_gives_way_to_the_checked_answer(offline):
    """A date is verified information too: the month name is a qualifier word, so the repair must keep it."""
    offline.texts = [SOURCE + " הנכס נבדק במרץ."]
    first = "הנכס נבדק במרץ [S1].\nהנכס מושכר [S1]."
    repaired = "הנכס פנוי [S1]."
    out = _turn(ScriptedAgent([SEARCH, final(first), final(repaired)], judge=_judge_wrong_management))
    assert "במרץ" in out.answer.answer_markdown and "פנוי" not in out.answer.answer_markdown
    assert [p["check"] for r in out.rounds for p in r].count("repair_lost_supported") == 1


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


def test_a_negation_that_qualifies_a_positive_claim_is_protected_but_an_absence_statement_is_not():
    ws = _ws(SOURCE + " המחיר הוא 55 ₪ ואינו כולל מע״מ. הסכום לא חושב.")
    report = verify_answer(_judge(), _answer("המחיר הוא 55 ₪ ואינו כולל מע״מ [S1].\nהסכום לא חושב [S1]."), ws, "?", [])
    assert not report.lost_supported("המחיר הוא 55 ₪ ואינו כולל מע״מ [S1].")  # nothing lost: the absence is free
    (lost,) = report.lost_supported("המחיר הוא 55 ₪ [S1].")
    assert "מע״מ" in lost[1].text  # dropping "אינו כולל מע״מ" from a supported price claim is a loss
    assert not report.lost_supported("המחיר הוא 55 ₪ ואינו כולל מע״מ [S1].\nהסכום חושב והוא 3 ₪ [S1].")
    for text in ("הקומה אינה כלולה בשטח", "אין אפשרות לחשב את הסכום", "לא ניתן לקבוע את השווי", "לא נמצא שטח"):
        assert verify.states_absence(text) == (text != "הקומה אינה כלולה בשטח")


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

def test_the_judge_version_is_11_and_the_verdict_cache_key_changes_with_it(monkeypatch):
    assert verify.JUDGE_VERSION == 11  # round 16 U1: the read passages in the requirements call, `support`
    ws = _ws()
    unit = verify.split_units("השווי למ\"ר הוא 9,500 ₪ [S1].")[0]
    now = verify.unit_key(unit, ws, "?", "", {})
    monkeypatch.setattr(verify, "JUDGE_VERSION", 10)
    assert verify.unit_key(unit, ws, "?", "", {}) != now
