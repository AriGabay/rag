"""Round 8 reproductions (U1): one test per defect of the plan
``docs/plans/2026-10-11-0741-fix-claim-level-verification-final-text-plan.md`` (Problem Frame), each asserting the
TARGET behaviour of the requirement it belongs to and marked ``xfail(strict=True)`` until the unit that fixes it removes
the marker (a fix then shows as an unexpected pass) — the round-7 pattern. The one scenario that already holds on
the starting build (AE7, and the heading half of AE4) is an ordinary regression test and says so.

The model and the judge are scripted: these tests prove what the server does with what the judge returns, not the
judge's judgement. A scripted judge plays what the real one does with what it is shown (for an uncited unit: the
sources shown in the call, rule 9 of the judge policy). A field a later unit adds (a claim id, a final digest) is read
with ``.get`` and a default, so the setup never fails on its absence: the test fails on the behaviour. No database
is used: the engine runs offline with a scripted tool runner, as the round-7 offline reproductions do. Every text,
name and amount is synthetic."""

from __future__ import annotations

import hashlib
import json
import re
import uuid

import pytest

from app.chat import api, engine, verify
from app.chat import tools as T
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import VerdictCache, verify_answer
from app.db import TenantContext
from app.providers.llm import CallStatus, Purpose
from tests.support.scripted_agent import ScriptedAgent, call, component, final
from tests.support.scripted_provider import ScriptedProvider

SOURCE = "סיכום: השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש. הנכס פנוי."
# a sentence that says something is missing from the documents
GAP = re.compile(r"לא\s+(?:נמצא|נמצאה|נמצאו|נבדק|נבדקו|מופיע|מופיעים|אותר)")


# --- helpers -------------------------------------------------------------------------------------------------------

def _ws(*texts: str) -> Workspace:
    """A workspace holding one source per text (``S1``, ``S2``, ...), as a turn that read them would."""
    ws = Workspace(ctx=None)
    for t in texts:
        ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                      location="סעיף \"סיכום\"", kind="context", text=t)
    return ws


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def _units(input: str) -> dict[int, str]:
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def _cites(input: str) -> dict[int, list[str]]:
    return {int(i): [c for c in cites.split(",") if c]
            for i, cites in re.findall(r'<unit index="(\d+)" cites="([^"]*)">', input)}


def _shown(input: str) -> dict[str, str]:
    """The sources a judge call shows, by id."""
    return {sid: body for sid, body in re.findall(r'<source id="([^"]+)"[^>]*>\n(.*?)\n</source>', input, re.S)}


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d[\d,.]*\d|\d", text))


def _verdict(index: int, verdict: str, reason: str = "בדיקה", **kw) -> dict:
    return {"index": index, "verdict": verdict, "reason": reason} | kw


def _scripted(respond) -> ScriptedProvider:
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda instructions, input: respond(input), repeat=True)
    return p


def _one_unit_per_call(monkeypatch) -> None:
    """Each unit is judged in a call of its own, so a test can fail one call or look at one unit's input."""
    monkeypatch.setattr(verify, "JUDGE_MAX_UNITS", 1)


# --- AE1 (R8, R9, R10): a sentence is never removed whole when only part of it fails -----------------------------------

AREA = "השטח העיקרי בתא שטח 1 הוא 1,200 מ״ר"
AREA_SOURCE = "תא שטח 1: השטח העיקרי הוא 1,200 מ״ר."


@pytest.mark.parametrize("sentence, unsupported", [
    # the approval date is a number the source never states: the deterministic number check finds it, the judge splits
    # the unit (it is shown the number as <unstated>), and only the clause holding it is cut
    (f"{AREA}, שאושר ביום 1.1.2020 [S1].", "1.1.2020"),
    # the clause carries no number: the judge fails it (the source has no approval at all)
    (f"{AREA}, שאושר בוועדה המחוזית [S1].", "שאושר בוועדה"),
], ids=["number_clause", "judge_clause"])
def test_ae1_a_mixed_sentence_keeps_its_supported_claim_and_cuts_only_the_failing_part(sentence, unsupported):
    clause = sentence.removeprefix(AREA + ", ").removesuffix(" [S1].")

    def respond(input: str) -> dict:  # the real judge finds the area supported and the approval absent: a mixed unit
        return {"verdicts": [{"index": i, "verdict": "partial", "reason": "תאריך האישור אינו במקור", "defect": "part",
                              "claims": [{"span": AREA, "verdict": "supported"},
                                         {"span": clause, "verdict": "unsupported", "failure": "absent_from_source"}]}
                             for i in _units(input)]}

    answer = _answer(sentence)
    report = verify_answer(_scripted(respond), answer, _ws(AREA_SOURCE), "מה השטח העיקרי בתא שטח 1?", [])
    final_text = report.apply(answer).answer_markdown
    assert "1,200" in final_text and "תא שטח 1" in final_text and "מ״ר" in final_text, final_text
    assert unsupported not in final_text and "חלק שלא אומת הושמט" in final_text, final_text
    assert report.ok  # nothing for a repair round: the cut is the server's


# --- AE2 (R2): a supported but uncited datum is repaired with a citation, never removed ----------------------------

VALUE_SOURCE = "סיכום: השווי למ\"ר הוא 9,500 ₪."
RENT_SOURCE = "נספח השכרה: דמי השכירות הם 55 ₪ למ\"ר לחודש."


def test_ae2_an_uncited_datum_supported_by_a_source_read_in_the_turn_gets_its_citation_and_is_not_removed():
    def respond(input: str) -> dict:
        """What the real judge does: a cited unit is supported; an uncited one only by a shown source that states
        its number (named in ``supported_by``); with none shown, a factual claim without a source is unsupported."""
        shown, cites, out = _shown(input), _cites(input), []
        for i, text in _units(input).items():
            if cites[i]:
                out.append(_verdict(i, "supported"))
                continue
            by = [sid for sid, body in shown.items() if _numbers(text) <= _numbers(body)]
            out.append(_verdict(i, "supported", supported_by=by) if by
                       else _verdict(i, "unsupported", "טענה עובדתית בלי מקור", failure="absent_from_source"))
        return {"verdicts": out}

    # S1 is cited by the first sentence; S2 was read in the turn, supports the second, and no unit cites it
    answer = _answer("השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש.")
    report = verify_answer(_scripted(respond), answer, _ws(VALUE_SOURCE, RENT_SOURCE), "מה השווי ודמי השכירות?", [])
    final_text = report.apply(answer).answer_markdown
    assert report.counts()["removed"] == 0, report.counts()
    assert "55" in final_text and "[S2]" in final_text, final_text  # kept, with the citation attached
    assert not [p for p in report.problems if p.removes_unit], [p.reason for p in report.problems]


# --- AE3 (R6, R14, R19): a technical failure of the check is never a removal for lack of support ----------------------

CHECK_FAILED = "דמי השכירות הם 55 ₪ למ\"ר לחודש [S1]."
OMITTED = "הנכס פנוי [S1]."
MISCLASSIFIED = "דמי השכירות 55 ₪ הם, כך נראה, נתון חשוב [S1]."
CONTENT_FAILURES = set(verify.FAILURE_KINDS) - {"not_checked"}


def test_ae3_a_judge_batch_failing_twice_is_withheld_with_its_own_reason_and_the_rest_stays_verified(monkeypatch):
    _one_unit_per_call(monkeypatch)

    def respond(input: str):
        texts = _units(input)
        if any("דמי השכירות הם 55" in t for t in texts.values()):
            return CallStatus.TIMEOUT  # this batch fails, again on its retry
        out = []
        for i, t in texts.items():
            if t.startswith("הנכס פנוי"):
                continue  # the judge leaves this unit out, each time it is asked
            out.append(_verdict(i, "not_factual" if "כך נראה" in t else "supported"))
        return {"verdicts": out}

    answer = _answer(f"השווי למ\"ר הוא 9,500 ₪ [S1].\n{CHECK_FAILED}\n{OMITTED}\n{MISCLASSIFIED}")
    report = verify_answer(_scripted(respond), answer, _ws(SOURCE), "?", [])
    final_text = report.apply(answer).answer_markdown
    assert "9,500" in final_text  # the rest of the answer is shown verified
    removed = {p.unit.text.rstrip(" ."): p for p in reversed(report.problems) if p.removes_unit}
    keys = [t.replace(" [S1].", "") for t in (CHECK_FAILED, OMITTED, MISCLASSIFIED)]
    by_text = {t: removed[t] for t in keys if t in removed}
    assert len(by_text) == 3, [(p.unit.text, p.failure_kind) for p in report.problems]
    failed = by_text[keys[0]]
    # never called wrong: not contradicted, not absent from the source, not any content failure
    assert failed.failure_kind not in CONTENT_FAILURES, failed.failure_kind
    # the three causes stay distinct (R14): the failed call has a reason, and a sentence on the user's path, of its own
    reasons = {(p.failure_kind, p.summary()["text"]) for p in by_text.values()}
    assert len(reasons) == 3, {t: (p.failure_kind, p.summary()["text"]) for t, p in by_text.items()}


# --- AE4 (R7, R16, R18): the judge sees the heading above a unit --------------------------------------------------

UNDER_HEADING = "## מצב מוצע\nתכסית 40% [S1]."
COVERAGE_SOURCE = "תכסית 40% מותרת."  # says nothing of the state: the heading is the answer's only context


def _recording_judge(seen: list[str]) -> ScriptedProvider:
    def respond(input: str) -> dict:
        seen.append(input)
        return {"verdicts": [_verdict(i, "supported") for i in _units(input)]}

    return _scripted(respond)


def test_ae4_the_judge_input_for_a_sentence_carries_the_heading_above_it(monkeypatch):
    _one_unit_per_call(monkeypatch)  # the heading is judged in a call of its own: the sentence's call is only its own
    seen: list[str] = []
    verify_answer(_recording_judge(seen), _answer(UNDER_HEADING), _ws(COVERAGE_SOURCE), "?", [])
    (call_input,) = [i for i in seen if "תכסית 40%" in _units(i).get(1, "")]
    assert "מצב מוצע" in call_input, call_input


def test_ae4_a_changed_heading_context_forces_a_new_verdict_already_holds_on_9da69c1():
    """Already passing: ``unit_key`` includes the heading above the unit, so a verdict is not reused under another
    heading. It stays as a regression test beside the failing half above (what the judge is shown)."""
    seen: list[str] = []
    cache = VerdictCache()
    judge = _recording_judge(seen)
    ws = _ws(COVERAGE_SOURCE)
    verify_answer(judge, _answer(UNDER_HEADING), ws, "?", [], cache=cache)
    assert len(seen) == 1
    # an edit removes the heading line: the same sentence now sits under no heading
    again = verify_answer(judge, _answer("תכסית 40% [S1]."), ws, "?", [], cache=cache)
    assert len(seen) == 2 and again.reused == 0, (len(seen), again.reused)
    # the same answer unchanged is reused, with no new call
    verify_answer(judge, _answer("תכסית 40% [S1]."), ws, "?", [], cache=cache)
    assert len(seen) == 2


# --- the engine offline: scripted tools, no database ---------------------------------------------------------------

def _ctx() -> TenantContext:
    return TenantContext(office_id=uuid.uuid4(), user_id=uuid.uuid4(), role="admin")


@pytest.fixture
def offline(monkeypatch):
    """The turn without a database: a search registers the synthetic passage (``offline.text``) as a source; the
    coverage ledger (which looks titles up) is replaced by ``offline.coverage``, a function ``(answer) -> note`` whose
    note (a coverage note, when it returns one) is appended as the real ``coverage.build`` does."""
    doc, ver = uuid.uuid4(), uuid.uuid4()

    def run_tool(ws, name, arguments):
        src = ws.add_source(document_id=doc, version_id=ver, title="דוח בדיקה", section="סיכום",
                            location="סעיף \"סיכום\"", kind="context", text=run_tool.text)
        for reason in run_tool.limits:  # a limit the reading reached: the turn states it after the answer
            if reason not in ws.limits_hit:
                ws.limits_hit.append(reason)
        return f'<source id="{src.sid}">{src.text}</source>'

    def build(ws, answer, question):
        note = run_tool.coverage(answer)
        if note:
            answer = answer.model_copy(update={"answer_markdown": answer.answer_markdown.rstrip() + "\n\n> " + note,
                                               "status": "partial" if answer.status == "answered" else answer.status})
        return {}, answer

    run_tool.text, run_tool.limits, run_tool.coverage = SOURCE, [], lambda answer: None
    monkeypatch.setattr(T, "run_tool", run_tool)
    monkeypatch.setattr(engine.coverage, "build", build)
    return run_tool


def _turn(agent: ScriptedAgent, question: str) -> engine.TurnOutcome:
    inp = engine.TurnInput(question=question, history=[], summary=None, focus_documents=[], prior_refs={})
    return engine.run_turn(_ctx(), agent, inp, lambda *a: None, lambda: False)


SEARCH = [call("search", query="שווי", document_ids=None, limit=None)]


def _payload(out: engine.TurnOutcome) -> dict:
    """What the API stores and returns for the turn (``api._answer_payload``), with the normal path's view of its
    verification (``_message_json``)."""
    payload = api._answer_payload(out)
    return payload | {"verification": api.public_verification(payload["verification"])}


# --- AE6 (R16, R17): the saved text is the checked text, recorded with a digest ----------------------------------------

AE6_ANSWER = "השווי למ\"ר הוא 9,500 ₪ [S1]."
COMPONENT = "מספר החדרים בנכס"


@pytest.mark.xfail(strict=True, reason="U6/U7 (R16, R17): the gap paragraph, the limit sentence and the coverage note "
                                       "are added after the last verification and nothing checks or records the result: "
                                       "the payload has no verification.final_digest")
def test_ae6_text_added_after_verification_is_saved_checked_and_recorded_with_a_digest(offline):
    offline.limits = [engine.STEP_LIMIT]
    offline.coverage = lambda answer: "**כיסוי:** לא כל המסמכים המתאימים נבדקו."
    asks = [("השווי למ\"ר", "9,500", []), (COMPONENT, None, ["S1"])]

    def judge(input: str) -> dict:
        units = _units(input)
        out = {"verdicts": [_verdict(i, "supported") for i in units]}
        if "<derive_requirements>" in input or "<requirements>" in input:
            frozen = {t: i for i, t in re.findall(r'<requirement id="([A-Z][\d.]+)"[^>]*>\n(.*?)\n</requirement>',
                                                  input, re.S)}
            out["requirements"] = [{"id": frozen.get(ask, ""), "text": "" if frozen else ask, "calculation": False,
                                    "status": "full" if hits else "missing", "units": hits, "related": [] if hits
                                    else related, "reason": "בדיקה"}
                                   for ask, keyword, related in asks
                                   for hits in [[i for i, t in units.items() if keyword and keyword in t]]]
        return out

    agent = ScriptedAgent([SEARCH, final(AE6_ANSWER), final(AE6_ANSWER), final(AE6_ANSWER)], judge=judge,
                          request=[component(asks[0][0]), component(COMPONENT)])
    out = _turn(agent, "מה השווי למ\"ר ומה מספר החדרים בנכס?")
    markdown = out.answer.answer_markdown
    # the three server edits are in the saved text (what makes the answer differ from the one that was checked)
    assert engine.LIMIT_SENTENCES[engine.STEP_LIMIT] in markdown, markdown  # the limit sentence
    assert "**כיסוי:**" in markdown, markdown  # the coverage note
    assert COMPONENT in markdown, markdown  # the gap paragraph
    verification = _payload(out)["verification"]
    assert verification.get("final_digest") == hashlib.sha256(markdown.encode()).hexdigest(), verification


# --- AE8 (R22, R23): a removal detail is linked to its claim by id, not by position ---------------------------------

TWO_WRONG = ("השווי למ\"ר הוא 9,500 ₪ [S1].\n"
             "דמי השכירות הם 1,234 ₪ למ\"ר לחודש [S1].\n"
             "דמי הניהול הם 777 ₪ למ\"ר לחודש [S1].")


@pytest.mark.xfail(strict=True, reason="U3/U7/U8 (R22, R23, KTD8): the removal summaries (verification.removals) and the "
                                       "decisions (diagnostics.removed) carry no claim id; Message.tsx pairs them by "
                                       "list position")
def test_ae8_removal_summaries_and_decisions_are_paired_by_claim_id_not_by_position(offline):
    offline.text = SOURCE
    agent = ScriptedAgent([SEARCH, final(TWO_WRONG), CallStatus.ERROR])  # the repair round fails: the checked one stays
    out = _turn(agent, "מה השווי למ\"ר ודמי השכירות והניהול?")
    summaries = _payload(out)["verification"]["removals"]
    decisions = api._diagnostics(out, {})["removed"]
    assert len(summaries) == len(decisions) == 2, (summaries, decisions)
    # each side names its claim: the same ids, one for each
    ids = [s.get("claim_id") for s in summaries]
    assert all(ids) and len(set(ids)) == 2, summaries
    assert sorted(ids) == sorted(d.get("claim_id") or "" for d in decisions), decisions
    # the list is reordered (the UI orders claims as the answer shows them): the id still finds the right decision
    reordered = list(reversed(decisions))
    by_id = {d.get("claim_id"): d for d in reordered}
    for s in summaries:
        assert by_id[s["claim_id"]]["failure_kind"] == s["failure_kind"], (s, by_id[s["claim_id"]])
    texts = {s["claim_id"]: by_id[s["claim_id"]]["text"] for s in summaries}
    assert "1,234" in texts[ids[0]] and "777" in texts[ids[1]], texts


# --- AE7 (R20): a category partly shown names only its missing component ------------------------------------------------

AREAS_SOURCE = "תא שטח 1: השטחים העיקריים הם 1,200 מ״ר."
AREAS_ANSWER = "השטחים העיקריים בתא שטח 1 הם 1,200 מ״ר [S1]."
CATEGORY = "השטחים בתא שטח 1"
MAIN, SERVICE = "השטחים העיקריים בתא שטח 1", "שטחי השירות בתא שטח 1"


def test_ae7_a_component_missing_from_a_category_is_named_alone_already_holds_on_9da69c1(offline):
    """Already passing on 9da69c1 (round 7 named the exact missing component). Kept as a regression test: the main
    areas are shown, so the category is not declared missing and only the service areas are listed, with a reason."""
    offline.text = AREAS_SOURCE
    scores = [(CATEGORY, None, ["S1"]), (MAIN, "1,200", []), (SERVICE, None, ["S1"])]  # the judge scores the parent missing

    def judge(input: str) -> dict:
        units = _units(input)
        out = {"verdicts": [_verdict(i, "supported") for i in units]}
        if "<derive_requirements>" in input or "<requirements>" in input:
            frozen = {t: i for i, t in re.findall(r'<requirement id="([A-Z][\d.]+)"[^>]*>\n(.*?)\n</requirement>',
                                                  input, re.S)}
            out["requirements"] = [{"id": frozen.get(ask, ""), "text": "" if frozen else ask, "calculation": False,
                                    "status": "full" if hits else "missing", "units": hits,
                                    "related": [] if hits else related, "reason": "בדיקה"}
                                   for ask, keyword, related in scores
                                   for hits in [[i for i, t in units.items() if keyword and keyword in t]]]
        return out

    analysed = [component(CATEGORY, id="1", conditional=True), component(MAIN, id="1.1", parent="1"),
                component(SERVICE, id="1.2", parent="1")]
    agent = ScriptedAgent([SEARCH, final(AREAS_ANSWER), final(AREAS_ANSWER), final(AREAS_ANSWER)], judge=judge,
                          request=analysed)
    out = _turn(agent, f"פרט את {CATEGORY}: עיקריים ושירות.")
    markdown = out.answer.answer_markdown
    assert "1,200" in markdown
    gaps = [line for line in markdown.splitlines() if GAP.search(line) or "שטחי השירות" in line]
    assert [g for g in gaps if SERVICE in g], markdown  # the missing component is named, with its place
    assert not [g for g in gaps if CATEGORY in g or MAIN in g], markdown  # the category and the shown part are not
    missing = (out.report.counts().get("completeness") or {}).get("missing") or []
    assert [m["text"] for m in missing] == [SERVICE], missing
    assert missing[0]["reason"], missing  # with its reason
    assert json.dumps(missing, ensure_ascii=False).find("1,200") == -1
