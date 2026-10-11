"""The final check of the exact saved text (round 8 U6, KTD6, KTD7, KTD10; R3, R16–R19, R28, R29).

The engine runs offline — a scripted model and judge, a scripted tool runner that registers synthetic passages as
sources, no database — so a test proves what the server does with what the judge returns, how many judge calls the final
check costs (``verify_final``) and what is saved, not the judge's judgement. Every text, name and amount is synthetic."""

from __future__ import annotations

import hashlib
import re
import uuid
from decimal import Decimal

import pytest

from app.chat import api, calc, engine, final_check, resolve, verify
from app.chat import tools as T
from app.chat.compose import text_digest
from app.db import TenantContext
from app.providers.llm import CallStatus
from tests.support.scripted_agent import ScriptedAgent, call, component, final
from tests.unit.test_verify_computed import QUESTION as RATE_QUESTION
from tests.unit.test_verify_computed import _compute, _value

AREA = "השטח העיקרי בתא שטח 1 הוא 1,200 מ״ר"
CLAUSE = "שאושר בוועדה המחוזית"
MIXED = f"{AREA}, {CLAUSE} [S1]."  # the area is in the source; the approval is not
VALUE = "השווי למ\"ר הוא 9,500 ₪ [S1]."
SOURCE = "תא שטח 1: השטח העיקרי הוא 1,200 מ״ר. תכסית 40% מותרת. השווי למ\"ר הוא 9,500 ₪. הנכס פנוי."
SEARCH = [call("search", query="שטח", document_ids=None, limit=None)]
MARKER = "חלק שלא אומת הושמט"


# --- the harness: the engine offline ------------------------------------------------------------------------------

def _units(input: str) -> dict[int, str]:
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def _verdict(index: int, verdict: str, reason: str = "בדיקה", **kw) -> dict:
    return {"index": index, "verdict": verdict, "reason": reason} | kw


class Judge:
    """A scripted judge: ``rule(text, input, n)`` answers each unit (a dict of verdict fields, or a verdict name), ``n``
    being the number of the call (1 = the first verification). A rule may return ``CallStatus`` for the whole call via
    ``fail(n)``. With the turn's requirements in the input, each is scored by ``asks`` (component text -> keyword): given in
    full by the units of the call that hold the keyword and were judged supported."""

    def __init__(self, rule, fail=lambda n, input: None, asks: dict[str, str] | None = None):
        self.rule, self.fail, self.asks = rule, fail, asks or {}
        self.inputs: list[str] = []

    def __call__(self, input: str):
        self.inputs.append(input)
        n = len(self.inputs)
        status = self.fail(n, input)
        if status is not None:
            return status
        out, verdicts = [], {}
        for i, t in _units(input).items():
            r = self.rule(t, input, n)
            r = {"verdict": r} if isinstance(r, str) else r
            verdicts[i] = r["verdict"]
            out.append(_verdict(i, **r))
        result: dict = {"verdicts": out}
        if "<requirements>" in input or "<derive_requirements>" in input:
            frozen = {t: i for i, t in re.findall(r'<requirement id="([A-Z][\d.]+)"[^>]*>\n(.*?)\n</requirement>',
                                                  input, re.S)}
            units = _units(input)
            result["requirements"] = [
                {"id": rid, "text": "", "calculation": False, "status": "full" if hits else "missing", "units": hits,
                 "related": [], "reason": "בדיקה"}
                for text, rid in frozen.items()
                for hits in [[i for i, t in units.items() if self.asks.get(text) and self.asks[text] in t
                              and verdicts[i] in ("supported", "partial")]]]
        return result

    @property
    def final_inputs(self) -> list[str]:
        return [i for i in self.inputs if "<server>" in i or "<unit_context" in i]


@pytest.fixture
def offline(monkeypatch):
    """The turn without a database: a search registers the synthetic passages (``offline.texts``, one source and one
    document each) as sources; the coverage ledger's lookups are replaced by ``offline.coverage``, a function
    ``(answer) -> note`` whose note, when it returns one, is appended as the real ``coverage.build`` does."""

    def run_tool(ws, name, arguments):
        if not ws.sources:
            for text in run_tool.texts:
                ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                              location="סעיף \"סיכום\"", kind="context", text=text)
            for reason in run_tool.limits:
                if reason not in ws.limits_hit:
                    ws.limits_hit.append(reason)
            run_tool.setup(ws)
        return "".join(f'<source id="{s.sid}">{s.text}</source>' for s in ws.sources.values())

    def build(ws, answer, question):
        note = run_tool.coverage(answer)
        if note:
            answer = answer.model_copy(update={"answer_markdown": answer.answer_markdown.rstrip() + "\n\n> " + note,
                                               "status": "partial" if answer.status == "answered" else answer.status})
        return {}, answer

    run_tool.texts, run_tool.limits, run_tool.coverage, run_tool.setup = [SOURCE], [], (lambda answer: None), \
        (lambda ws: None)
    monkeypatch.setattr(T, "run_tool", run_tool)
    monkeypatch.setattr(engine.coverage, "build", build)
    return run_tool


def _turn(agent, question: str = "מה השטח העיקרי?", history=()) -> engine.TurnOutcome:
    inp = engine.TurnInput(question=question, history=list(history), summary=None, focus_documents=[], prior_refs={})
    ctx = TenantContext(office_id=uuid.uuid4(), user_id=uuid.uuid4(), role="admin")
    return engine.run_turn(ctx, agent, inp, lambda *a: None, lambda: False)


def _purposes(out: engine.TurnOutcome) -> list[str]:
    return [u["purpose"] for u in out.usage]


def _kinds(out: engine.TurnOutcome) -> list[str]:
    return [s.kind for s in out.server_spans]


def _mixed_rule(text: str, input: str, n: int):
    """The first verification splits the mixed sentence (the area supported, the approval not); every other sentence is
    supported; the sentence as it stands after the cut (the marker in it) is supported too."""
    if n == 1 and CLAUSE in text:
        return {"verdict": "partial", "defect": "part", "claims": [
            {"span": AREA, "verdict": "supported"},
            {"span": CLAUSE, "verdict": "unsupported", "failure": "absent_from_source"}]}
    return "supported"


# --- AE6 (R16, R17): the saved text is the checked text ---------------------------------------------------------------

def test_text_added_after_the_check_is_saved_with_its_digest_and_costs_no_judge_call(offline):
    """A gap paragraph, a limit sentence and a coverage note are server text: checked by construction, by the fixed point
    — no judge call — and the digest is that of the saved bytes."""
    offline.limits = [engine.STEP_LIMIT]
    offline.coverage = lambda answer: "**כיסוי:** לא כל המסמכים המתאימים נבדקו."
    room = "מספר החדרים בנכס"
    judge = Judge(lambda t, i, n: "supported", asks={"השווי למ\"ר": "9,500", room: "חדרים"})
    agent = ScriptedAgent([SEARCH, final(VALUE), final(VALUE), final(VALUE)], judge=judge,
                          request=[component("השווי למ\"ר"), component(room)])
    out = _turn(agent, "מה השווי למ\"ר ומה מספר החדרים בנכס?")
    md = out.answer.answer_markdown
    assert _kinds(out) == ["gap_paragraph", "limit_sentence", "coverage_note"], md
    assert room in md and engine.LIMIT_SENTENCES[engine.STEP_LIMIT] in md and "**כיסוי:**" in md
    assert out.final_digest == hashlib.sha256(md.encode()).hexdigest() == text_digest(md)
    assert "verify_final" not in _purposes(out)
    assert out.final_check["judge_calls"] == 0 and out.final_check["passes"] == 1
    # outside the server's spans there is only the model's sentence
    assert "".join(md[a:b] for a, b in zip([0, *(s.end for s in out.server_spans)],
                                           [*(s.start for s in out.server_spans), len(md)], strict=True)) == VALUE


def test_a_removal_a_partial_mark_and_a_qualifier_cost_no_judge_call(offline):
    offline.texts = ["השווי למ\"ר בנוי ברוטו למסחר הוא 9,500 ₪, ללא מע\"מ. דמי השכירות הם 55 ₪ למ\"ר לחודש. הנכס פנוי."]
    answer = "השווי למ\"ר הוא 9,500 ₪, ללא מע\"מ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].\nהנכס מושכר [S1]."

    def rule(text, input, n):
        if "מושכר" in text:
            return {"verdict": "unsupported", "failure": "absent_from_source"}
        if "דמי השכירות" in text:
            return {"verdict": "partial", "defect": "none"}  # no concrete defect: marked, nothing to repair
        return "supported"

    agent = ScriptedAgent([SEARCH, final(answer), final(answer), final(answer)], judge=Judge(rule))
    out = _turn(agent, "מה השווי, דמי השכירות והאם הנכס מושכר?")
    md = out.answer.answer_markdown
    assert "מושכר" not in md and "אומת חלקית" in md and "מ״ר בנוי ברוטו, כפי שנכתב במקור" in md
    assert {"partial_mark", "qualifier", "removal_note"} <= set(_kinds(out)), _kinds(out)
    assert "verify_final" not in _purposes(out) and out.final_check["judge_calls"] == 0
    assert out.final_digest == text_digest(md)


def test_a_citation_the_server_attaches_from_the_judges_support_reuses_its_verdict(offline):
    """KTD7: the inserted citation is a server span, the key of the sentence does not change, the verdict is reused."""
    offline.texts = ["סיכום: השווי למ\"ר הוא 9,500 ₪.", "נספח: דמי השכירות הם 55 ₪ למ\"ר לחודש."]

    def rule(text, input, n):
        return {"verdict": "supported", "supported_by": ["S2"]} if "55" in text else "supported"

    answer = "השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש."
    out = _turn(ScriptedAgent([SEARCH, final(answer)], judge=Judge(rule)), "מה השווי ודמי השכירות?")
    assert "55 ₪ למ\"ר לחודש [S2]" in out.answer.answer_markdown and "citation" in _kinds(out)
    assert "verify_final" not in _purposes(out)


# --- the cost of a changed unit: one judge call, labelled ---------------------------------------------------------------

def test_a_sentence_with_a_span_cut_is_judged_once_more_as_the_text_that_is_saved(offline):
    judge = Judge(_mixed_rule)
    out = _turn(ScriptedAgent([SEARCH, final(MIXED)], judge=judge))
    md = out.answer.answer_markdown
    assert _purposes(out) == ["agent", "request", "agent", "verify", "verify_final"]
    assert "1,200" in md and "ועדה" not in md and MARKER in md
    # the judge read the sentence as saved, with the server's marker shown as the server's
    assert f"<server>«[1: {MARKER}]»</server>" in judge.inputs[-1]
    assert _units(judge.inputs[-1]) and CLAUSE not in judge.inputs[-1].split("<unit ")[1]
    assert out.final_check["judge_calls"] == 1 and out.final_digest == text_digest(md)
    (record,) = [r for r in out.report.claims[0] if r.verdict == "supported"]
    assert record.post_check == "supported" and record.action == "kept"


def test_a_cut_whose_remainder_is_not_supported_is_removed_and_the_answer_is_composed_again(offline):
    """The ladder's last step: the cut sentence is judged as saved and fails — a lost qualifier, say — so the unit goes,
    the removal note is the server's again, and the second pass finds nothing new to judge."""
    def rule(text, input, n):
        return {"verdict": "unsupported", "failure": "absent_from_source"} if MARKER in text else _mixed_rule(text, input, n)

    out = _turn(ScriptedAgent([SEARCH, final(f"{MIXED}\n{VALUE}")], judge=Judge(rule)))
    md = out.answer.answer_markdown
    assert "1,200" not in md and "9,500" in md and "הוסרו מהתשובה 1 טענות" in md
    assert _purposes(out).count("verify_final") == 1 and out.final_check["passes"] == 2
    assert out.final_check["removed"] == 1 and _kinds(out) == ["removal_note"]
    (p,) = out.report.removals()
    assert p.check == "final" and p.failure_kind == "absent_from_source"
    assert {r.action for r in out.report.claims[0]} == {"removed"}
    assert out.final_digest == text_digest(md)


# --- AE4 (R7, R16, R18): the heading above a sentence is part of what was checked -------------------------------------------

def test_removing_a_heading_line_judges_the_sentence_below_again_and_proposed_is_not_shown_as_approved(offline):
    offline.texts = ["תכסית 40% מותרת."]

    def rule(text, input, n):
        if "מצב מוצע" in text and "תכסית" not in text:
            return {"verdict": "unsupported", "failure": "absent_from_source"}  # the heading line itself fails
        if "תכסית" in text:  # supported only under the heading it was written under
            return "supported" if "<heading>מצב מוצע</heading>" in input else {"verdict": "unsupported"}
        return "supported"

    answer = "## מצב מוצע\nתכסית 40% [S1]."
    judge = Judge(rule)
    out = _turn(ScriptedAgent([SEARCH, final(answer), final(answer), final(answer)], judge=judge), "מה התכסית?")
    md = out.answer.answer_markdown
    assert "תכסית" not in md and "מצב מוצע" not in md  # never the sentence without the heading it was written under
    assert _purposes(out).count("verify_final") == 1
    assert "<heading>" not in judge.inputs[-1] and "תכסית 40%" in judge.inputs[-1]  # judged with no heading above it
    assert out.final_check["removed"] == 1 and out.final_digest == text_digest(md)


# --- AE3 (R14, R19): a technical failure of the final check is never a removal for lack of support -------------------------

def _two_sentences(offline):
    offline.texts = [SOURCE]
    return f"{MIXED}\n{VALUE}"


def test_when_the_final_check_judge_fails_the_changed_sentence_is_withheld_and_the_rest_is_kept(offline):
    answer = _two_sentences(offline)
    judge = Judge(_mixed_rule, fail=lambda n, input: CallStatus.TIMEOUT if n > 1 else None)
    out = _turn(ScriptedAgent([SEARCH, final(answer)], judge=judge))
    md = out.answer.answer_markdown
    assert "9,500" in md and "1,200" not in md and "ועדה" not in md
    assert _purposes(out)[-2:] == ["verify_final", "verify_final"]  # the call and its one retry
    (p,) = out.report.removals()
    assert p.failure_kind == "not_checked" and p.cause == "check_failed" and p.check == "final"
    assert p.summary()["text"] == verify.CHECK_CAUSE_SENTENCES["check_failed"]
    records = out.report.claims[0]
    assert {r.action for r in records} == {"withheld"} and not any(r.verdict == "contradicts" for r in records)
    assert out.final_check["unavailable"] and out.final_check["withheld"] == 1
    assert out.final_digest == text_digest(md)


def test_without_time_the_changed_sentence_is_withheld_with_no_judge_call(offline, monkeypatch):
    answer = _two_sentences(offline)
    monkeypatch.setattr(final_check, "FINAL_MIN_SECONDS", 10**6)
    out = _turn(ScriptedAgent([SEARCH, final(answer)], judge=Judge(_mixed_rule)))
    md = out.answer.answer_markdown
    assert "9,500" in md and "1,200" not in md
    assert "verify_final" not in _purposes(out)
    (p,) = out.report.removals()
    assert p.failure_kind == "not_checked" and p.cause == "check_failed"


# --- the fixed point (KTD6) -----------------------------------------------------------------------------------------------

def test_a_removal_in_the_final_pass_changes_a_components_status_and_the_gap_paragraph_follows(offline):
    area, value = "השטח העיקרי בתא שטח 1", "השווי למ\"ר"

    def rule(text, input, n):
        return {"verdict": "unsupported", "failure": "absent_from_source"} if MARKER in text else _mixed_rule(text, input, n)

    judge = Judge(rule, asks={area: "1,200", value: "9,500"})
    out = _turn(ScriptedAgent([SEARCH, final(f"{MIXED}\n{VALUE}")], judge=judge,
                              request=[component(area), component(value)]), "מה השטח העיקרי ומה השווי למ\"ר?")
    md = out.answer.answer_markdown
    by_id = {o["text"]: o for o in out.ledger["requirements"]}
    assert by_id[area]["status"] == "not_answered" and by_id[value]["status"] == "full"
    # the first composition stated no gap (the area was given); the saved one does, and says what the removal did
    assert "gap_paragraph" in _kinds(out) and "removal_note" in _kinds(out)
    gap = next(md[s.start:s.end] for s in out.server_spans if s.kind == "gap_paragraph")
    assert area in gap
    assert out.final_check["passes"] == 2 and out.final_check["judge_calls"] == 1
    assert out.final_digest == text_digest(md)
    for s in out.server_spans:
        assert 0 <= s.start < s.end <= len(md)


def test_a_last_pass_heading_removal_withholds_the_sentence_below_it_and_recomputes_the_server_text(offline, monkeypatch):
    monkeypatch.setattr(final_check, "FINAL_PASSES", 1)
    offline.texts = ["נתוני תכנון: מצב מוצע. תכסית 40% מותרת.", "השווי למ\"ר הוא 9,500 ₪."]
    heading = "## נתוני תכנון: מצב מוצע ושטח 900 דונם [S1]"
    answer = f"{VALUE.replace('[S1]', '[S2]')}\n{heading}\nתכסית 40% [S1]."  # the value is above the heading

    def rule(text, input, n):
        if text.startswith("## נתוני תכנון"):
            if n == 1:  # the area of the plot is no where in the source: that clause is cut
                return {"verdict": "partial", "defect": "part", "claims": [
                    {"span": "נתוני תכנון: מצב מוצע", "verdict": "supported"},
                    {"span": "ושטח 900 דונם", "verdict": "unsupported", "failure": "absent_from_source"}]}
            return {"verdict": "unsupported", "failure": "absent_from_source"}  # as saved, the heading is not supported
        return "supported"

    judge = Judge(rule)
    out = _turn(ScriptedAgent([SEARCH, final(answer)], judge=judge), "מה התכסית והשווי?")
    md = out.answer.answer_markdown
    # the last pass removed the heading; the sentence below it had a verdict only under that heading
    assert "תכסית" not in md and "נתוני תכנון" not in md and "9,500" in md
    assert _purposes(out).count("verify_final") == 1  # the cascade costs no judge call
    assert out.final_check["withheld"] == 2
    withheld = {p.unit.text: p for p in out.report.removals()}
    below = next(p for t, p in withheld.items() if t.startswith("תכסית"))
    assert below.failure_kind == "not_checked" and below.cause == "check_failed" and below.check == "final"
    # the server's spans were recomputed after the withholding: the removal note counts the heading (not supported as
    # saved) and the sentence (not checked under its new heading) apart, and the registry indexes the saved text exactly
    (note,) = out.server_spans
    assert note.kind == "removal_note" and md[note.end:] == "" and md[:note.start] == "השווי למ\"ר הוא 9,500 ₪ [S2]."
    assert md[note.start:note.end].count("הוסרו מהתשובה 1 טענות") == 2
    assert out.final_digest == text_digest(md)


def test_the_documents_cited_in_the_final_text_are_marked_verified_once_and_removals_keep_every_pass(offline, monkeypatch):
    offline.texts = ["נתוני תכנון: מצב מוצע.", "תכסית 40% מותרת.", "השווי למ\"ר הוא 9,500 ₪."]
    answer = (VALUE.replace("[S1]", "[S3]") + "\n## נתוני תכנון: מצב מוצע ושטח 900 דונם [S1]\nתכסית 40% [S2].")

    def rule(text, input, n):
        if text.startswith("## נתוני תכנון"):
            return ({"verdict": "partial", "defect": "part", "claims": [
                {"span": "נתוני תכנון: מצב מוצע", "verdict": "supported"},
                {"span": "ושטח 900 דונם", "verdict": "unsupported", "failure": "absent_from_source"}]} if n == 1
                    else {"verdict": "unsupported", "failure": "absent_from_source"})
        if "תכסית" in text:  # pass 1 still has the heading above it; pass 2 (it went) does not
            return "supported" if "<heading>" in input else {"verdict": "unsupported", "failure": "absent_from_source"}
        return "supported"

    touched: list[str] = []
    real = engine.coverage.touch_verified
    monkeypatch.setattr(engine.coverage, "touch_verified", lambda ws, md: (touched.append(md), real(ws, md)))
    out = _turn(ScriptedAgent([SEARCH, final(answer)], judge=Judge(rule)), "מה התכסית והשווי?")
    ws, md = out.workspace, out.answer.answer_markdown
    assert out.final_check["passes"] == 2 and _purposes(out).count("verify_final") == 2
    assert "[S3]" in md and "[S1]" not in md and "[S2]" not in md
    assert touched == [md]  # once, for the final text
    doc = {sid: str(s.document_id) for sid, s in ws.sources.items()}
    assert ws.activity[doc["S3"]]["level"] == "verified"
    assert not ws.activity.get(doc["S1"], {}).get("level") == "verified"
    assert not ws.activity.get(doc["S2"], {}).get("level") == "verified"
    # every document behind a removal — the heading's (pass 1) and the sentence's (pass 2) — is behind the message
    removals = api._removals(ws, out.report)
    assert {doc["S1"], doc["S2"]} <= api._removal_documents(removals)


def test_what_rests_on_a_unit_the_final_check_removes_goes_with_it(offline):
    """A conclusion whose verdict rests on the sentence the final check removed (R11) — with no judge call for it."""
    conclusion = "מכאן שהשטח העיקרי הוא שטח גדול [S1]."

    def rule(text, input, n):
        if MARKER in text:
            return {"verdict": "unsupported", "failure": "absent_from_source"}
        if text.startswith("מכאן"):
            return {"verdict": "supported", "depends_on": [0]}
        return _mixed_rule(text, input, n)

    out = _turn(ScriptedAgent([SEARCH, final(f"{MIXED}\n{conclusion}\n{VALUE}")], judge=Judge(rule)))
    md = out.answer.answer_markdown
    assert "מכאן" not in md and "1,200" not in md and "9,500" in md and "הוסרו מהתשובה 2 טענות" in md
    gone = {p.unit.text.split(",")[0].split(" ")[0]: p for p in out.report.removals()}
    assert gone["מכאן"].check == "dependency" and gone["מכאן"].failure_kind == gone["השטח"].failure_kind
    assert _purposes(out).count("verify_final") == 1 and out.final_digest == text_digest(md)


def test_a_table_left_without_its_header_goes_with_it(offline, monkeypatch):
    """The intro line goes (nothing supports it); the header, judged again with no intro above it, fails in the last pass;
    its rows go with it — one table is never left headless."""
    monkeypatch.setattr(final_check, "FINAL_PASSES", 1)
    table = "| נתון בנכס | ערך בש״ח |\n|---|---|\n| השווי למ\"ר | 9,500 ₪ [S1] |"
    answer = f"הטבלה מציגה את הנתונים העיקריים של הנכס [S1].\n{table}\n\n{VALUE}"

    def rule(text, input, n):
        if text.startswith("הטבלה מציגה"):
            return {"verdict": "unsupported", "failure": "absent_from_source"}
        if "נתון בנכס" in text:  # the header is supported only under the line that introduces the table
            return "supported" if "<table_intro>" in input else {"verdict": "unsupported"}
        return "supported"

    out = _turn(ScriptedAgent([SEARCH, final(answer), final(answer), final(answer)], judge=Judge(rule)))
    md = out.answer.answer_markdown
    assert "9,500 ₪ [S1]" in md and "|" not in md and "נתון בנכס" not in md  # the sentence stays; the table is gone whole
    rows = [p for p in out.report.removals() if "השווי למ" in p.unit.text and p.unit.table_span]
    assert rows and rows[0].check == "dependency" and rows[0].cause == ""
    assert _purposes(out).count("verify_final") == 1 and out.final_digest == text_digest(md)


def test_a_component_given_only_by_the_part_a_cut_took_is_no_longer_given(offline):
    """KTD9: a unit that voted for a component and was cut is judged with the requirements block, and the votes are
    replaced by that call's scores — the gap paragraph then names the component the cut took away."""
    approval, area = "אישור התוכנית", "השטח העיקרי"
    judge = Judge(_mixed_rule, asks={approval: "שאושר", area: "1,200"})
    agent = ScriptedAgent([SEARCH, final(f"{MIXED}\n{VALUE}")], judge=judge,
                          request=[component(approval), component(area)])
    out = _turn(agent, "מה השטח העיקרי ומה אישור התוכנית?")
    assert "<requirements>" in judge.inputs[-1] and "<requirements>" in judge.inputs[0]
    by_text = {o["text"]: o for o in out.ledger["requirements"]}
    assert by_text[approval]["status"] == "not_answered" and by_text[area]["status"] == "full"
    md = out.answer.answer_markdown
    gap = next(md[s.start:s.end] for s in out.server_spans if s.kind == "gap_paragraph")
    assert approval in gap and area not in gap
    assert out.final_check["passes"] == 2 and out.final_check["judge_calls"] == 1
    assert out.final_digest == text_digest(md)


def test_a_unit_judged_again_only_to_be_scored_with_the_one_it_voted_with_is_kept_when_the_check_fails(offline):
    """KTD9 with a failed call: the unchanged unit that shares a component with a cut one keeps its verdict."""
    shared = "הנתונים העיקריים של הנכס"
    judge = Judge(_mixed_rule, asks={shared: "הוא"}, fail=lambda n, input: CallStatus.TIMEOUT if n > 1 else None)
    out = _turn(ScriptedAgent([SEARCH, final(f"{MIXED}\n{VALUE}")], judge=judge, request=[component(shared)]),
                "מהם הנתונים העיקריים של הנכס?")
    md = out.answer.answer_markdown
    assert "9,500" in md and "1,200" not in md
    assert "<requirements>" in judge.inputs[-1] and VALUE.split(" [")[0] in judge.inputs[-1]  # scored with it
    (p,) = out.report.removals()
    assert p.failure_kind == "not_checked" and p.unit.text.startswith("השטח העיקרי")
    assert out.final_check["withheld"] == 1


def test_a_cut_of_a_unit_that_voted_for_nothing_asks_for_no_requirements(offline):
    judge = Judge(_mixed_rule)
    _turn(ScriptedAgent([SEARCH, final(MIXED)], judge=judge))
    assert len(judge.inputs) == 2 and "<requirements>" not in judge.inputs[-1]


# --- the fallbacks that return an earlier round's answer are composed and checked like any other ---------------------------

FIRST = f"{MIXED}\nהנכס מושכר [S1]."  # a cut, and a sentence nothing supports (a repair round is asked)


def _fallback_rule(text, input, n):
    return {"verdict": "unsupported", "failure": "absent_from_source"} if "מושכר" in text else _mixed_rule(text, input, n)


def _assert_fallback_checked(out):
    md = out.answer.answer_markdown
    assert "1,200" in md and MARKER in md and "מושכר" not in md and "הוסרו מהתשובה 1 טענות" in md
    assert _purposes(out)[-1] == "verify_final" and out.final_digest == text_digest(md)
    assert {"marker", "removal_note"} <= set(_kinds(out))


def test_a_repair_step_that_fails_falls_back_to_the_checked_answer_which_is_composed_and_checked(offline):
    out = _turn(ScriptedAgent([SEARCH, final(FIRST), CallStatus.ERROR], judge=Judge(_fallback_rule)))
    _assert_fallback_checked(out)


def test_an_invalid_repair_answer_falls_back_the_same_way(offline):
    bad = {"final": {"status": "answered"}}  # not the schema
    out = _turn(ScriptedAgent([SEARCH, final(FIRST), bad], judge=Judge(_fallback_rule)))
    _assert_fallback_checked(out)


def test_a_verification_that_cannot_run_in_a_repair_round_falls_back_the_same_way(offline):
    repaired = final(f"{MIXED}\n{VALUE}")  # a new sentence: the second verification has a unit to judge, and fails

    judge = Judge(_fallback_rule, fail=lambda n, input: CallStatus.TIMEOUT if n in (2, 3) else None)
    out = _turn(ScriptedAgent([SEARCH, final(FIRST), repaired], judge=judge))
    _assert_fallback_checked(out)


def test_no_time_for_a_second_verification_falls_back_the_same_way(offline):
    from app.config import get_settings

    agent = ScriptedAgent([SEARCH, final(FIRST), final(FIRST)], judge=Judge(_fallback_rule))
    settings = get_settings()
    wanted = settings.chat_verify_min_seconds

    def squeeze(step: int) -> None:
        if step == 2:  # the repair round's answer arrives with less time than a verification needs
            settings.chat_verify_min_seconds = 10**6

    agent.on_step = squeeze
    try:
        out = _turn(agent)
    finally:
        settings.chat_verify_min_seconds = wanted
    _assert_fallback_checked(out)


# --- R29: a clarification keeps its question and its status ---------------------------------------------------------------

QUESTION = "מהו שיעור ההיוון שברצונך להשתמש בו?"


def test_a_clarification_whose_claims_all_fail_keeps_its_question_and_its_status(offline):
    answer = final(f"הנכס מושכר [S1].\n{QUESTION}", status="clarification", clarification=QUESTION)

    def rule(text, input, n):
        return {"verdict": "unsupported", "failure": "absent_from_source"} if "מושכר" in text else "not_factual"

    out = _turn(ScriptedAgent([SEARCH, answer, answer, answer], judge=Judge(rule)), "מה שווי הנכס?")
    md = out.answer.answer_markdown
    assert out.answer.status == "clarification" and QUESTION in md and "מושכר" not in md
    assert "לא הצלחתי" not in md and "removal_note" in _kinds(out)
    assert out.final_digest == text_digest(md)


def test_a_clarification_question_that_cites_a_source_is_never_cut(offline):
    question = "האם להשתמש בשיעור 40% [S1] כשיעור ההיוון?"
    answer = final(f"הנכס מושכר [S1].\n{question}", status="clarification", clarification=question)

    def rule(text, input, n):  # the judge would fail the question too, as an unverifiable sentence
        return {"verdict": "unsupported", "failure": "absent_from_source"}

    out = _turn(ScriptedAgent([SEARCH, answer, answer, answer], judge=Judge(rule)), "מה שווי הנכס?")
    assert out.answer.status == "clarification" and "האם להשתמש בשיעור 40%" in out.answer.answer_markdown
    assert "מושכר" not in out.answer.answer_markdown


def test_a_clarification_of_the_question_alone_costs_no_final_judge_call(offline):
    answer = final(QUESTION, status="clarification", clarification=QUESTION)
    out = _turn(ScriptedAgent([SEARCH, answer], judge=Judge(lambda t, i, n: "not_factual")), "מה שווי הנכס?")
    assert out.answer.status == "clarification" and out.answer.answer_markdown == QUESTION
    assert "verify_final" not in _purposes(out) and out.final_digest == text_digest(QUESTION)


def test_a_server_clarification_is_saved_with_a_digest_and_no_judge_call(offline, monkeypatch):
    asked = "לא מצאתי נכס בשם הזה. לאיזה נכס הכוונה?"
    monkeypatch.setattr(engine.resolve, "resolve", lambda *a, **k: resolve.Request(
        kind="new_question", standalone_question="?", clarify=asked, server_clarify=True))
    agent = ScriptedAgent([], judge=Judge(lambda t, i, n: "supported"))
    history = [engine.HistoryMessage("user", "מה השווי?"), engine.HistoryMessage("assistant", "השווי הוא 9,500 ₪.")]
    out = _turn(agent, "ומה בנכס אחר?", history)
    assert out.answer.status == "clarification" and out.answer.answer_markdown == asked
    assert out.final_digest == text_digest(asked) and out.server_spans == []
    assert not any(u["purpose"] in ("verify", "verify_final") for u in out.usage)


# --- R29: a value the user gave earlier in the conversation is a valid computation input ----------------------------------

def test_a_computation_that_uses_a_value_the_user_gave_earlier_survives_the_final_check(offline):
    earlier = "נניח שהעלויות יעלו ב-5%"

    def setup(ws):
        ws.values.clear()
        _value(ws, "12,450,000", "סה״כ הכנסות", "income", "income")
        _value(ws, "10,400,000", "סה״כ עלויות", "cost", "cost")
        # the user's own number, from an EARLIER message of the conversation (turn 1; this turn's is the second)
        ws.assumptions["A1"] = calc.Assumption("A1", Decimal("5"), "5", "percent", "עליית העלויות",
                                               "העלויות יעלו ב-5%", 1, False)
        _compute(ws, "V1 - V2*(1+A1%)", "הרווח בתרחיש")

    offline.setup = setup
    answer = "לפי הנחתך שהעלויות יעלו ב-5% [A1], הרווח בתרחיש יהיה 1,530,000 ₪ [V1][V2]."
    history = [engine.HistoryMessage("user", earlier), engine.HistoryMessage("assistant", "אפשר לחשב תרחיש.")]
    out = _turn(ScriptedAgent([SEARCH, final(answer)], judge=Judge(lambda t, i, n: "supported")),
                RATE_QUESTION, history)
    ws, md = out.workspace, out.answer.answer_markdown
    assert not ws.assumptions["A1"].current and ws.assumptions["A1"].quote in earlier
    assert not out.report.removed_units(), out.report.problems_text()
    assert "1,530,000 ₪ [V1][V2][C1]" in md and "[A1]" in md  # kept, and its calculation cited by the server
    inputs = {i["id"]: i["kind"] for i in ws.computations["C1"].inputs}
    assert inputs["A1"] == "assumption"
    assert "verify_final" not in _purposes(out) and out.final_digest == text_digest(md)


# --- R28: the conversation's focus comes from the claims that survived -----------------------------------------------------

def test_the_focus_names_only_documents_the_surviving_text_cites(offline):
    offline.texts = ["השווי למ\"ר הוא 9,500 ₪.", "דמי השכירות הם 55 ₪ למ\"ר לחודש."]
    answer = "השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 77 ₪ למ\"ר לחודש [S2]."
    docs: dict[str, str] = {}

    def setup(ws):
        docs.update({sid: str(s.document_id) for sid, s in ws.sources.items()})

    offline.setup = setup

    def focus():
        return {"metric_as_written": "שווי למ\"ר", "metric_kind": "unknown", "unit": "unknown", "period": "unknown",
                "area_basis": "", "vat": "unknown", "subject": "הנכס", "value_role": "unknown",
                "document_ids": [docs["S1"], docs["S2"]]}

    def rule(text, input, n):
        return {"verdict": "unsupported", "failure": "absent_from_source"} if "77" in text else "supported"

    agent = ScriptedAgent([SEARCH, lambda items: final(answer, focus=focus()), lambda items: final(answer, focus=focus()),
                           lambda items: final(answer, focus=focus())], judge=Judge(rule))
    out = _turn(agent, "מה השווי ודמי השכירות?")
    assert "77" not in out.answer.answer_markdown
    assert out.answer.focus.document_ids == [docs["S1"]]  # the removed claim's document is not what the talk is about
    only_removed = "דמי השכירות הם 77 ₪ למ\"ר לחודש [S2]."
    step = lambda items: final(only_removed, focus=focus() | {"document_ids": [docs["S2"]]})  # noqa: E731
    again = _turn(ScriptedAgent([SEARCH, step, step, step], judge=Judge(rule)), "מה דמי השכירות?")
    assert again.answer.focus is None  # nothing it rested on survived


# --- a partly given component names its missing part (round 8 R20, AE7): the line is a server span of the fixed point ----

class PartialJudge(Judge):
    """Scores the one component ``partial``, given by the units that hold ``keyword``, and names ``missing`` as the part
    of it the answer does not give (the judge's own word, validated by the server against the request)."""

    def __init__(self, rule, keyword: str, missing: str):
        super().__init__(rule)
        self.keyword, self.missing = keyword, missing

    def __call__(self, input: str):
        out = super().__call__(input)
        if isinstance(out, dict) and "<requirements>" in input:
            units = [i for i, t in _units(input).items() if self.keyword in t]
            out["requirements"] = [{"id": r["id"], "text": "", "calculation": False, "status": "partial",
                                    "units": units, "related": [], "reason": "בדיקה", "missing": self.missing}
                                   for r in out["requirements"]]
        return out


SERVICE_Q = "מהם שטחי השירות של תא שטח 1 ושל תא שטח 2?"
SERVICE_PART = "שטחי השירות בתא שטח 1"
SERVICE_2 = "שטחי השירות בתא שטח 2 הם 300 מ״ר"


def test_a_missing_part_of_a_partial_component_is_one_gap_line_that_is_a_fixed_point_of_the_final_state(offline):
    offline.texts = [f"תא שטח 2: {SERVICE_2.split(' בתא')[0]} הם 300 מ״ר. {SOURCE}"]
    judge = PartialJudge(lambda text, input, n: "supported", "300", SERVICE_PART)
    out = _turn(ScriptedAgent([SEARCH, final(f"{SERVICE_2} [S1].")], judge=judge,
                              request=[component("שטחי שירות")]), SERVICE_Q)
    md = out.answer.answer_markdown
    (o,) = out.ledger["requirements"]
    assert (o["status"], o["missing_part"], o["stated"]) == ("partial", SERVICE_PART, True)
    gap = [md[s.start:s.end].strip() for s in out.server_spans if s.kind == "gap_paragraph"]
    assert len(gap) == 1 and gap[0].count(SERVICE_PART) == 1 and gap[0].startswith(f"**{SERVICE_PART}**"), gap
    assert md.count(SERVICE_PART) == 1 and out.answer.status == "partial"
    assert out.final_check["judge_calls"] == 0 and out.final_digest == text_digest(md)  # server text: no judge call
    assert out.report.completeness["missing"][0]["missing_part"] == SERVICE_PART


def test_the_missing_part_survives_the_final_checks_vote_replacement(offline):
    """A cut of a unit that voted for the component: the votes are replaced by the final call's scores, the missing part
    is validated again and the gap line follows from the new vote."""
    judge = PartialJudge(_mixed_rule, "1,200", "השטח העיקרי בתא שטח 2")
    out = _turn(ScriptedAgent([SEARCH, final(f"{MIXED}")], judge=judge, request=[component("השטח העיקרי")]),
                "מהו השטח העיקרי בתא שטח 1 ובתא שטח 2?")
    assert "<requirements>" in judge.inputs[-1] and out.final_check["judge_calls"] == 1
    md = out.answer.answer_markdown
    (o,) = out.ledger["requirements"]
    assert (o["status"], o["missing_part"]) == ("partial", "השטח העיקרי בתא שטח 2")
    gap = next(md[s.start:s.end] for s in out.server_spans if s.kind == "gap_paragraph")
    assert "השטח העיקרי בתא שטח 2" in gap and out.final_digest == text_digest(md)
