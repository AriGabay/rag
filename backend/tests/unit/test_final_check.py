"""The final check of the exact saved text (round 8 U6, KTD6, KTD7, KTD10; R3, R16–R19, R28, R29).

The engine runs offline — a scripted model and judge, a scripted tool runner that registers synthetic passages as
sources, no database — so a test proves what the server does with what the judge returns, how many judge calls the final
check costs (``verify_final``) and what is saved, not the judge's judgement. Every text, name and amount is synthetic."""

from __future__ import annotations

import dataclasses
import hashlib
import re
import time
import uuid
from decimal import Decimal

import pytest

from app.chat import api, calc, engine, final_check, resolve, verify
from app.chat import tools as T
from app.chat.compose import Composition, ServerSpan, text_digest
from app.chat.engine import FinalAnswer
from app.chat.verify import Expansion
from app.db import TenantContext
from app.providers.llm import CallStatus
from tests.support.scripted_agent import ScriptedAgent, call, component, final
from tests.unit.test_verify_computed import QUESTION as RATE_QUESTION
from tests.unit.test_verify_computed import _compute, _value
from tests.unit.test_verify_reread import FakeFetcher

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


def test_without_time_a_cut_that_carried_an_approval_withholds_the_sentence_with_a_time_limit_cause(offline, monkeypatch):
    """Round 9 U15 (R19, AE6): the cut clause ("שאושר בוועדה המחוזית") is a status word, so the supported remainder is not
    kept unjudged; the unit is withheld and the removal says the check ran out of time — not a technical failure."""
    answer = _two_sentences(offline)
    monkeypatch.setattr(final_check, "FINAL_MIN_SECONDS", 10**6)
    out = _turn(ScriptedAgent([SEARCH, final(answer)], judge=Judge(_mixed_rule)))
    md = out.answer.answer_markdown
    assert "9,500" in md and "1,200" not in md
    assert "verify_final" not in _purposes(out)
    (p,) = out.report.removals()
    assert p.failure_kind == "not_checked" and p.cause == "time_limit" and p.check == "final"
    sentence = p.summary()["text"]
    assert sentence == verify.CHECK_CAUSE_SENTENCES["time_limit"] and "מגבלת הזמן" in sentence
    assert "תקלה" not in sentence and "סותר" not in sentence
    assert {r.action for r in out.report.claims[0]} == {"withheld"}
    assert out.final_check["withheld"] == 1 and out.final_digest == text_digest(md)


PLAIN_CUT = "והנכס מושכר לדייר מסחרי"  # a clause that qualifies nothing: no negation, condition, status, date or caveat


def _plain_cut_rule(text, input, n):
    if n == 1 and PLAIN_CUT in text:
        return {"verdict": "partial", "defect": "part", "claims": [
            {"span": AREA, "verdict": "supported"},
            {"span": PLAIN_CUT, "verdict": "unsupported", "failure": "absent_from_source"}]}
    return "supported"


def test_without_time_the_supported_remainder_of_a_cut_sentence_is_kept_when_the_cut_clause_qualified_nothing(
        offline, monkeypatch):
    """AE6: the draft judged the area supported and cut a clause that holds no qualifier; the final check has no time to
    judge the sentence as saved, so it keeps the remainder with no judge call, and nothing is withheld."""
    offline.texts = [SOURCE]
    monkeypatch.setattr(final_check, "FINAL_MIN_SECONDS", 10**6)
    out = _turn(ScriptedAgent([SEARCH, final(f"{AREA}, {PLAIN_CUT} [S1].\n{VALUE}")], judge=Judge(_plain_cut_rule)))
    md = out.answer.answer_markdown
    assert "1,200" in md and "9,500" in md and PLAIN_CUT not in md and MARKER in md
    assert "verify_final" not in _purposes(out)
    assert not out.report.removals() and out.final_check["withheld"] == 0 and out.final_check["unjudged_kept"] == 1
    assert {r.action for r in out.report.claims[0] if r.verdict == "supported"} == {"kept"}
    assert out.final_digest == text_digest(md)


@pytest.mark.parametrize("clause", [
    "וזהו מצב מוצע", "ואין לו היתר", "בכפוף לאישור הוועדה", "ללא היתר בנייה", "בהנחה שהשטח לא ישתנה", "והוא הופקד במרץ",
    "כ-15 יחידות דיור", "עד 12.03.2024", "וטרם אושר"])
def test_without_time_a_cut_clause_with_a_qualifier_word_withholds_the_unit(offline, monkeypatch, clause):
    """The lexicon (``verify.QUALIFIER_LEXICON``): a negation, a condition, a status, an assumption, a date or an
    approximation in the cut span — the remainder could read as stronger than the draft, so it is withheld."""
    offline.texts = [SOURCE]
    monkeypatch.setattr(final_check, "FINAL_MIN_SECONDS", 10**6)

    def rule(text, input, n):
        if n == 1 and clause in text:
            return {"verdict": "partial", "defect": "part", "claims": [
                {"span": AREA, "verdict": "supported"}, {"span": clause, "verdict": "unsupported"}]}
        return "supported"

    out = _turn(ScriptedAgent([SEARCH, final(f"{AREA}, {clause} [S1].\n{VALUE}")], judge=Judge(rule)))
    md = out.answer.answer_markdown
    assert "1,200" not in md and "9,500" in md and "verify_final" not in _purposes(out)
    (p,) = out.report.removals()
    assert p.cause == "time_limit" and out.final_check["unjudged_kept"] == 0


def test_without_time_a_remainder_is_not_kept_when_a_standing_claim_was_only_partly_supported(offline, monkeypatch):
    offline.texts = [SOURCE]
    monkeypatch.setattr(final_check, "FINAL_MIN_SECONDS", 10**6)

    def rule(text, input, n):
        if n == 1 and PLAIN_CUT in text:
            return {"verdict": "partial", "defect": "part", "claims": [
                {"span": AREA, "verdict": "partial"}, {"span": PLAIN_CUT, "verdict": "unsupported"}]}
        return "supported"

    out = _turn(ScriptedAgent([SEARCH, final(f"{AREA}, {PLAIN_CUT} [S1].\n{VALUE}")], judge=Judge(rule)))
    assert "1,200" not in out.answer.answer_markdown
    assert [p.cause for p in out.report.removals()] == ["time_limit"]


def test_without_time_a_cut_heading_never_lets_the_sentence_below_it_stay_unjudged(offline, monkeypatch):
    """The safety case of the heading-removal test, with no time to judge again: the heading line is cut ("ושטח 900 דונם"
    holds no qualifier, so it stays), but the heading the sentence below was judged under changed — so the sentence is
    withheld with the time-limit cause and "proposed" is never read as approved."""
    offline.texts = ["נתוני תכנון: מצב מוצע. תכסית 40% מותרת. שטח 900 דונם."]
    monkeypatch.setattr(final_check, "FINAL_MIN_SECONDS", 10**6)
    heading = "## נתוני תכנון: מצב מוצע ושטח 900 דונם"

    def rule(text, input, n):
        if text.startswith("## נתוני תכנון") and n == 1:
            return {"verdict": "partial", "defect": "part", "claims": [
                {"span": "נתוני תכנון: מצב מוצע", "verdict": "supported"},
                {"span": "ושטח 900 דונם", "verdict": "unsupported", "failure": "absent_from_source"}]}
        return "supported"

    judge = Judge(rule)
    out = _turn(ScriptedAgent([SEARCH, final(f"{heading} [S1]\nתכסית 40% [S1].")], judge=judge), "מה התכסית?")
    md = out.answer.answer_markdown
    assert "תכסית" not in md and "verify_final" not in _purposes(out)
    assert out.final_check["unjudged_kept"] == 1  # the heading line's own remainder: its cut span qualified nothing
    (p,) = out.report.removals()
    assert p.unit.text.startswith("תכסית") and p.cause == "time_limit"
    assert out.final_digest == text_digest(md)


# --- ``remainder_stands`` itself: each guard that keeps a cut sentence unjudged fails closed -----------------------------------

class _Remainder:
    """A final check with one draft unit whose plain clause was cut (``PLAIN_CUT``, which qualifies nothing) and one unit
    of the saved text that says what is left; ``saved`` and the draft's fields are changed by each case. No time to judge."""

    DRAFT = f"{AREA}, {PLAIN_CUT} [S1]."
    SAVED = f"{AREA}."

    def __init__(self, monkeypatch, *, saved: str | None = None, drafts: list[int] | None = None, **draft_fields):
        from types import SimpleNamespace

        cut_from = self.DRAFT.index(f", {PLAIN_CUT}")
        draft = verify.Unit(0, self.DRAFT, self.DRAFT.replace(" [S1]", ""), ["S1"], 0, len(self.DRAFT))
        for name, value in draft_fields.items():
            setattr(draft, name, value)
        self.draft = draft
        report = SimpleNamespace(
            cuts={0: [verify.CutGroup(cut_from, cut_from + len(f", {PLAIN_CUT}"), ("c2",))]},
            claims={0: [SimpleNamespace(id="c1", verdict="supported"), SimpleNamespace(id="c2", verdict="unsupported")]})
        self.final = object.__new__(final_check._Final)
        self.final.report, self.final.drafts = report, {0: draft}
        self.final.stats = {"passes": 0, "units": 0, "reused": 0, "judged": 0, "removed": 0, "withheld": 0,
                            "unjudged_kept": 0, "unavailable": False}
        self.final.passed = set()
        self.final.deadline = time.monotonic() + 5  # less than the final check's reserve: no time to judge the saved text
        monkeypatch.setattr(final_check, "FINAL_MIN_SECONDS", 10**6)
        text = self.SAVED if saved is None else saved
        unit = verify.Unit(0, text, text, [], 0, len(text))
        unit.heading, unit.table_header, unit.context = draft.heading, draft.table_header, draft.context
        self.unit = final_check.FinalUnit(unit, True, [0] if drafts is None else drafts, "k", text=text)

    def check(self):
        self.final.split = lambda comp: [self.unit]
        return self.final.check(None, may_judge=False)


def test_a_cut_sentence_with_nothing_else_changed_is_kept_unjudged(monkeypatch):
    case = _Remainder(monkeypatch)
    assert case.final.remainder_stands(case.unit)
    assert case.check().failures == [] and case.final.stats["unjudged_kept"] == 1


@pytest.mark.parametrize("field, draft_value, saved_value", [
    ("context", "| שטח | ערך |\n|---|---|", "| שטח | שווי |\n|---|---|"),  # the table's header row changed
    ("context", "הטבלה מציגה את המצב הקיים", "הטבלה מציגה את המצב המוצע"),  # the line above the table changed
    ("table_header", False, True),  # the sentence became (or stopped being) a header row
    ("heading", "מצב קיים", "מצב מוצע"),
])
def test_a_changed_table_header_or_context_is_withheld_with_the_time_limit_cause(monkeypatch, field, draft_value, saved_value):
    """The cut clause qualifies nothing, yet the row means something else under another header or context: it must be
    judged, and with no time it is withheld — never kept unjudged."""
    case = _Remainder(monkeypatch, **{field: draft_value})
    setattr(case.unit.unit, field, saved_value)
    assert not case.final.remainder_stands(case.unit)
    out = case.check()
    assert [(f.kind, f.cause) for f in out.failures] == [("not_checked", "time_limit")]
    assert case.final.stats["unjudged_kept"] == 0


@pytest.mark.parametrize("saved, drafts, why", [
    (f"{AREA} והיא סופית.", [0], "a word added beyond the cut"),
    ("השטח העיקרי בתא שטח 1 הוא 1,300 מ״ר.", [0], "a number changed beyond the cut"),
    ("השטח הכולל בתא שטח 1 הוא 1,200 מ״ר.", [0], "a rewording beyond the cut"),
    (f"{AREA}.", [0, 1], "two drafts merged into one sentence"),
    (f"{AREA}.", [], "no draft behind it"),
])
def test_a_saved_sentence_that_differs_from_the_draft_by_more_than_the_cut_is_withheld(monkeypatch, saved, drafts, why):
    case = _Remainder(monkeypatch, saved=saved, drafts=drafts)
    assert not case.final.remainder_stands(case.unit), why
    out = case.check()
    assert [(f.kind, f.cause) for f in out.failures] == [("not_checked", "time_limit")], why
    assert case.final.stats["unjudged_kept"] == 0


def test_when_the_judge_fails_with_time_left_the_cause_stays_a_technical_failure(offline):
    """``time_limit`` is for a check with no time; a judge that fails is still ``check_failed`` (the existing test above
    pins the sentence)."""
    answer = _two_sentences(offline)
    judge = Judge(_mixed_rule, fail=lambda n, input: CallStatus.ERROR if n > 1 else None)
    out = _turn(ScriptedAgent([SEARCH, final(answer)], judge=judge))
    assert [p.cause for p in out.report.removals()] == ["check_failed"]


# --- the final check's reserve (round 9 R16, KTD9) ---------------------------------------------------------------------

class _Clock:
    """``time.monotonic`` moved forward by hand."""

    def __init__(self, monkeypatch) -> None:
        import time

        self.offset, real = 0.0, time.monotonic
        monkeypatch.setattr(time, "monotonic", lambda: real() + self.offset)


def _record_deadlines(monkeypatch, clock: _Clock, *, use_up: bool) -> dict:
    """The deadlines the engine hands the verification and the final check; ``use_up``: the verification runs until its
    own deadline, as a slow judge or a re-read would."""
    import time

    seen: dict = {}
    verify_answer, finalize = engine.verify_answer, engine.finalize

    def verifying(*args, **kwargs):
        seen["verify"] = kwargs["deadline"]
        report = verify_answer(*args, **kwargs)
        if use_up:
            clock.offset += max(0.0, kwargs["deadline"] - time.monotonic())
        return report

    def finalizing(*args, **kwargs):
        seen["final"] = kwargs["deadline"]
        return finalize(*args, **kwargs)

    monkeypatch.setattr(engine, "verify_answer", verifying)
    monkeypatch.setattr(engine, "finalize", finalizing)
    return seen


def test_the_verification_ends_a_reserve_before_the_final_check_and_the_allowance_end_is_the_final_checks(
        offline, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_final_reserve_seconds", 15)
    seen = _record_deadlines(monkeypatch, _Clock(monkeypatch), use_up=False)
    _turn(ScriptedAgent([SEARCH, final(VALUE)], judge=Judge(_mixed_rule)))
    assert seen["final"] - seen["verify"] == pytest.approx(15, abs=0.5)
    assert get_settings().chat_turn_seconds == 150  # the turn's total time is not raised


def test_a_verification_that_used_all_of_its_time_leaves_the_final_check_its_reserve_to_judge_the_cut_sentence(
        offline, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_final_reserve_seconds", 15)
    _record_deadlines(monkeypatch, _Clock(monkeypatch), use_up=True)
    out = _turn(ScriptedAgent([SEARCH, final(MIXED)], judge=Judge(_mixed_rule)))
    md = out.answer.answer_markdown
    # the final check still had its reserve: it judged the sentence as saved, and what it supports is kept
    assert "verify_final" in _purposes(out) and "1,200" in md and MARKER in md


def test_a_verification_with_less_than_its_minimum_before_its_own_deadline_fails_the_turn_though_the_allowance_goes_on(
        offline, monkeypatch):
    from app.config import get_settings

    for k, v in {"chat_final_reserve_seconds": 15, "chat_verify_min_seconds": 15}.items():
        monkeypatch.setattr(get_settings(), k, v)
    clock = _Clock(monkeypatch)
    agent = ScriptedAgent([SEARCH, final(VALUE)], judge=Judge(_mixed_rule))
    # the answer is written at 182 s: the allowance ends at 210 s (28 s left) but the verification's own deadline at 195 s
    agent.on_step = lambda i: setattr(clock, "offset", 182.0) if i == 1 else None
    with pytest.raises(engine.ProviderFailure) as exc:
        _turn(agent)
    assert exc.value.status == "verify_no_time"
    assert "verify" not in [c.purpose.value for c in agent.calls]


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


# --- round 17 U3: the item ledger in the final check (KTD3; R6, R8) ------------------------------------------------------------

RATES_TEXT = "שיעורי היוון לפי שימוש:\nמשרדים | 6.5%\nמסחר | 7.0%\nחניה | 8.25%"
RATES_ASK = "שיעורי ההיוון שנקבעו"


def _rate_item(label: str, quote: str, value: str, **kw) -> dict:
    return {"component": RATES_ASK, "label": label, "source": "S1", "quote": quote, "value": value, "condition": "",
            "state": "included", "reason": ""} | kw


def test_item_gap_lines_are_server_text_that_is_a_fixed_point_and_cost_no_judge_call(offline):
    """An item the answer does not give, one declared not verifiable and one declared not applicable are stated by the server
    in lines after the answer (``item_gap``): checked by construction, not judged, and the digest is that of the saved bytes. The
    answer is partial (the omitted item), and the line says where the item was read and that it was not used."""
    offline.texts = [RATES_TEXT]
    answer = "שיעור ההיוון למשרדים הוא 6.5% [S1]."
    items = [_rate_item("שיעור למשרדים", "משרדים | 6.5%", "6.5%"), _rate_item("שיעור למסחר", "מסחר | 7.0%", "7.0%"),
             _rate_item("שיעור לחניה", "חניה | 8.25%", "8.25%", state="not_verifiable", reason="הטבלה נקראה בקריאה לא ודאית")]
    judge = Judge(lambda t, i, n: "supported", asks={RATES_ASK: "6.5%"})
    out = _turn(ScriptedAgent([SEARCH, final(answer, items=items), final(answer, items=items), final(answer, items=items)],
                              judge=judge, request=[component(RATES_ASK)]), "מהם שיעורי ההיוון שנקבעו?")
    md = out.answer.answer_markdown
    assert _kinds(out) == ["item_gap"], md
    (span,) = out.server_spans
    lines = md[span.start:span.end].strip().splitlines()
    assert len(lines) == 2 and "שיעור למסחר" in lines[0] and "S1" in lines[0] and "לא נעשה בו שימוש" in lines[0]
    assert "שיעור לחניה" in lines[1] and "הטבלה נקראה בקריאה לא ודאית" in lines[1] and "S1" in lines[1]
    assert not re.search(r"לא נמצא|לא אותר|לא מופיע", md)
    assert out.answer.status == "partial" and md.startswith(answer)
    assert out.final_check["judge_calls"] == 0 and "verify_final" not in _purposes(out)
    assert out.final_digest == text_digest(md) == hashlib.sha256(md.encode()).hexdigest()
    states = {i["label"]: i["state"] for i in out.item_ledger["items"]}
    assert states == {"שיעור למשרדים": "covered", "שיעור למסחר": "omitted", "שיעור לחניה": "not_verifiable"}
    assert [d["title"] for d in out.ledger["gap_documents"]] == ["דוח בדיקה"]  # the place of the lines joins the permission set


def test_a_ledger_that_is_covered_or_explained_adds_no_line_and_keeps_the_answer_whole(offline):
    offline.texts = [RATES_TEXT]
    answer = "שיעור ההיוון למשרדים הוא 6.5% [S1]. שיעור ההיוון למסחר הוא 7.0% [S1]."
    items = [_rate_item("שיעור למשרדים", "משרדים | 6.5%", "6.5%"), _rate_item("שיעור למסחר", "מסחר | 7.0%", "7.0%")]
    judge = Judge(lambda t, i, n: "supported", asks={RATES_ASK: "6.5%"})
    out = _turn(ScriptedAgent([SEARCH, final(answer, items=items)], judge=judge, request=[component(RATES_ASK)]),
                "מהם שיעורי ההיוון שנקבעו?")
    assert _kinds(out) == [] and out.answer.answer_markdown == answer and out.answer.status == "answered"
    assert [i["state"] for i in out.item_ledger["items"]] == ["covered", "covered"]


def test_the_final_checks_requirements_call_shows_the_ledger_like_the_first_calls_do(offline):
    """A unit that voted for a component and was cut is judged again with the requirements block (KTD9): the block carries the
    turn's item ledger, as the first verification's does."""
    approval, area = "אישור התוכנית", "השטח העיקרי"
    item = {"component": area, "label": "שטח עיקרי", "source": "S1", "quote": "השטח העיקרי הוא 1,200 מ״ר",
            "value": "1,200", "condition": "", "state": "included", "reason": ""}
    judge = Judge(_mixed_rule, asks={approval: "שאושר", area: "1,200"})
    agent = ScriptedAgent([SEARCH, final(f"{MIXED}\n{VALUE}", items=[item])], judge=judge,
                          request=[component(approval), component(area)])
    out = _turn(agent, "מה השטח העיקרי ומה אישור התוכנית?")
    assert out.final_check["judge_calls"] == 1
    for call_input in (judge.inputs[0], judge.inputs[-1]):
        assert "<item_ledger>" in call_input and 'id="I1"' in call_input and "השטח העיקרי הוא 1,200" in call_input
    assert [i["id"] for i in out.item_ledger["items"]] == ["I1"]


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


# --- R23: one caveat per calculation is server text, a fixed point of the final state ----------------------------------------

def test_the_conditional_note_is_server_text_that_is_a_fixed_point_and_costs_no_judge_call(offline):
    """A result of a conditional calculation keeps a short tag; its reason is one server line after the body. Both are
    spans the final check takes by construction: no judge call, the digest of the saved bytes (R23, KTD6)."""
    def setup(ws):
        ws.values.clear()
        _value(ws, "15,600,000", "בנייה עילית", "cost", "cost")
        _value(ws, "4,620,000", "חניון תת-קרקעי", "cost", "cost")
        c = _compute(ws, "V1 + V2", "עלות הבנייה והחניון")
        c.outcome.conditional.append(T.MSG_UNCERTAIN_INPUTS.format(ids="V1, V2") + " (V1: התא לא אושר)")
        c.uncertain = ["V1", "V2"]

    offline.setup = setup
    answer = "עלות הבנייה העילית והחניון יחד היא 20,220,000 ₪ [C1].\nכלומר סך העלות הוא 20,220,000 ₪ [C1]."
    out = _turn(ScriptedAgent([SEARCH, final(answer), final(answer)], judge=Judge(lambda t, i, n: "supported")),
                "מה עלות הבנייה העילית והחניון יחד?")
    md = out.answer.answer_markdown
    note = "התוצאה C1 מותנית: הערכים V1, V2 אינם ודאיים."
    assert md.endswith("\n\n" + note) and md.count("אינם ודאיים") == 1, md
    assert md.count("(תוצאה מותנית)") == 2
    kinds = _kinds(out)
    assert kinds == ["qualifier", "qualifier", "conditional_note"], kinds  # in the order of the text
    assert out.final_check["judge_calls"] == 0 and "verify_final" not in _purposes(out)
    assert out.final_digest == text_digest(md) == hashlib.sha256(md.encode()).hexdigest()
    (span,) = [s for s in out.server_spans if s.kind == "conditional_note"]
    assert md[span.start:span.end] == "\n\n" + note


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


# --- R13, KTD5: what a re-read supplied is shown again where the sentence is judged as saved --------------------------

UNITS_NOTE = "הטבלה מוצגת במ״ר"


def test_a_claim_a_re_read_rescued_survives_the_cut_of_the_claim_beside_it_when_the_sentence_is_judged_as_saved(
        offline, monkeypatch):
    """The area is partly supported until the table's units note is read (``needs_context``); the approval is not
    supported at all and is cut. The sentence as saved holds the area and the marker, so its key is new and it is judged
    again — with the expansion that supported the area shown in its source, else the rescued claim would be lost with the
    cut one."""
    fetchers: list[FakeFetcher] = []  # the turn's fetcher, registering the expansion in the turn's own workspace
    monkeypatch.setattr(engine, "ContextReader", lambda ws: fetchers.append(
        FakeFetcher({("S1", "units_note"): Expansion(UNITS_NOTE, "יחידות")}, ws=ws)) or fetchers[-1])

    def rule(text, input, n):
        if "<server>" in input:  # the sentence as saved: supported only when the units note is shown
            return "supported" if UNITS_NOTE in input else {"verdict": "unsupported", "failure": "absent_from_source"}
        clause = {"span": CLAUSE, "verdict": "unsupported", "failure": "absent_from_source"}
        area = {"span": AREA, "verdict": "supported"} if UNITS_NOTE in input else {
            "span": AREA, "verdict": "partial", "needs_context": "units_note"}
        return {"verdict": "partial", "defect": "part", "claims": [area, clause]}

    judge = Judge(rule)
    out = _turn(ScriptedAgent([SEARCH, final(MIXED)], judge=judge))
    md = out.answer.answer_markdown
    assert _purposes(out) == ["agent", "request", "agent", "verify", "verify_reread", "verify_final"]
    (fetcher,) = fetchers
    assert fetcher.calls == [("S1", "units_note")] and fetcher.adopted == [("S1", "units_note")]
    assert "1,200" in md and CLAUSE not in md and MARKER in md, md  # only the cut part is gone
    assert out.final_check["removed"] == 0 and out.final_check["judge_calls"] == 1
    assert out.report.removals() == [] and out.final_digest == text_digest(md)
    shown = judge.inputs[-1]
    assert re.search(r'<source id="S1"[^>]*>.*<expanded_context kind="units_note">\n' + UNITS_NOTE, shown, re.S), shown
    assert [r.post_check for r in out.report.claims[0] if r.verdict == "supported"] == ["supported"]



# --- R19: nothing unchecked is returned — the failures of the final check end the turn as a retryable failure ------------

def _fails_closed(agent) -> engine.ProviderFailure:
    with pytest.raises(engine.ProviderFailure) as raised:
        _turn(agent)
    assert (raised.value.status, raised.value.detail) == ("verify_unavailable", "final_text")
    assert isinstance(raised.value.__cause__, final_check.FinalCheckError)
    return raised.value


def test_a_final_text_that_is_not_a_fixed_point_of_its_state_fails_the_turn_instead_of_saving_it(offline, monkeypatch):
    real, calls = final_check.compose, []

    def unstable(*args, **kwargs):  # the recomposition from the final state differs from the text that was checked
        comp = real(*args, **kwargs)
        calls.append(comp)
        return dataclasses.replace(comp, answer=comp.answer.model_copy(
            update={"status": "partial" if len(calls) % 2 == 0 else comp.answer.status}))

    monkeypatch.setattr(final_check, "compose", unstable)
    failure = _fails_closed(ScriptedAgent([SEARCH, final(VALUE)], judge=Judge(lambda t, i, n: "supported")))
    assert "fixed point" in str(failure.__cause__) and len(calls) >= 2


def test_a_unit_that_fails_and_cannot_be_removed_fails_the_turn_instead_of_saving_it(offline, monkeypatch):
    monkeypatch.setattr(final_check._Final, "apply", lambda self, failures, action: 0)

    def rule(text, input, n):  # the cut sentence fails as saved
        return {"verdict": "unsupported", "failure": "absent_from_source"} if MARKER in text else _mixed_rule(text, input, n)

    failure = _fails_closed(ScriptedAgent([SEARCH, final(MIXED)], judge=Judge(rule)))
    assert "could not be removed" in str(failure.__cause__)


def test_a_composition_that_does_not_index_its_text_fails_the_turn_instead_of_saving_it(offline, monkeypatch):
    real = final_check.compose
    monkeypatch.setattr(final_check, "compose", lambda *a, **kw: dataclasses.replace(real(*a, **kw), origin=[]))
    failure = _fails_closed(ScriptedAgent([SEARCH, final(VALUE)], judge=Judge(lambda t, i, n: "supported")))
    assert "does not index" in str(failure.__cause__)


# --- assert_fixed_point: the answer, the spans and the status must all be reproduced -----------------------------------

LIMIT_SPAN = (0, 4, "limit_sentence")


def _composition(markdown: str = VALUE, status: str = "answered", spans: list[ServerSpan] | None = None) -> Composition:
    answer = FinalAnswer(status=status, answer_markdown=markdown, items=[], claims=[], clarification_question="", missing_info="",
                         referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[], focus=None,
                         requested=[], parts=[])
    return Composition(answer, spans or [])


def test_a_recomposition_that_reproduces_the_text_its_spans_and_its_status_is_a_fixed_point():
    final_check.assert_fixed_point(_composition(spans=[ServerSpan(*LIMIT_SPAN)]),
                                   _composition(spans=[ServerSpan(*LIMIT_SPAN)]))


@pytest.mark.parametrize(("saved", "again"), [
    (_composition(), _composition(VALUE + " עוד")),
    (_composition(spans=[ServerSpan(*LIMIT_SPAN)]), _composition()),
    (_composition(spans=[ServerSpan(*LIMIT_SPAN)]), _composition(spans=[ServerSpan(0, 5, "limit_sentence")])),
    (_composition(spans=[ServerSpan(*LIMIT_SPAN)]), _composition(spans=[ServerSpan(0, 4, "coverage_note")])),
    (_composition(), _composition(status="partial")),
], ids=["answer", "span-missing", "span-offsets", "span-kind", "status"])
def test_a_recomposition_that_differs_in_text_spans_or_status_raises(saved, again):
    with pytest.raises(final_check.FinalCheckError):
        final_check.assert_fixed_point(saved, again)


# --- what the final judge's answer on the sentence as saved means (``decide``) ------------------------------------------

def _as_saved(reply):
    """A judge whose first answer cuts the clause of the mixed sentence and whose answer on the sentence as saved (the
    marker in it, shown as the server's) is ``reply``; ``reply`` None leaves the unit out of the reply."""

    def rule(text, input, n):
        return _mixed_rule(text, input, n) if "<server>" not in input else reply or "supported"

    class Omitting(Judge):
        def __call__(self, input: str):
            out = super().__call__(input)
            if "<server>" in input and reply is None:
                out["verdicts"] = []
            return out

    return (Omitting if reply is None else Judge)(rule)


UNSUPPORTED_PART = {"verdict": "partial", "defect": "part", "claims": [
    {"span": AREA, "verdict": "unsupported", "failure": "absent_from_source"}]}


@pytest.mark.parametrize(("reply", "kind", "cause", "check"), [
    ({"verdict": "not_factual"}, "not_checked", "not_classified", "final"),
    (None, "not_checked", "omitted", "final"),
    ({"verdict": "unsupported", "failure": "wrong_unit"}, "wrong_unit", "", "final"),
    ({"verdict": "contradicts"}, "contradicts_source", "", "final"),
    (UNSUPPORTED_PART, "absent_from_source", "", "final"),
    ({"verdict": "supported", "supported_by": ["S2"]}, "absent_from_source", "", "final"),
], ids=["not-factual", "no-verdict", "unsupported", "contradicts", "partial-with-unsupported-span", "unshown-support"])
def test_a_cut_sentence_the_final_judge_does_not_accept_is_removed_with_the_kind_and_cause_of_the_ruling(
        offline, reply, kind, cause, check):
    out = _turn(ScriptedAgent([SEARCH, final(f"{MIXED}\n{VALUE}")], judge=_as_saved(reply)))
    md = out.answer.answer_markdown
    assert "1,200" not in md and "9,500" in md and MARKER not in md, md  # the cut sentence went whole; the rest stays
    (p,) = out.report.removals()
    assert (p.failure_kind, p.cause, p.check) == (kind, cause, check), p
    assert {r.action for r in out.report.claims[0]} <= {"removed", "withheld"}
    assert out.final_check["removed"] + out.final_check["withheld"] == 1
    assert out.final_digest == text_digest(md)


def test_a_cut_sentence_judged_partly_supported_with_no_defect_passes_marked_as_partly_verified(offline):
    judge = _as_saved({"verdict": "partial", "defect": "none"})
    out = _turn(ScriptedAgent([SEARCH, final(MIXED)], judge=judge))
    md = out.answer.answer_markdown
    assert "1,200" in md and MARKER in md and "אומת חלקית" in md, md
    assert _kinds(out).count("partial_mark") == 1 and out.report.removals() == []
    assert any(p.check == "final" and p.reason == final_check.FINAL_PARTIAL for p in out.report.problems)
    assert _purposes(out).count("verify_final") == 1 and out.final_digest == text_digest(md)


# --- a grounded number is never removed because of an attribute (round 13 V2/W10, settled decision) -----------------------

VAT_SOURCE = "השווי למ\"ר הוא 9,500 ₪. הנכס פנוי."  # states no VAT
VAT_CLAIM = "השווי למ\"ר הוא 9,500 ₪, ללא מע\"מ [S1]."  # verification cuts the VAT phrase (a qualifier edit), then the final judge reads it


def _edited_then(final_reply: dict):
    """A judge that supports the sentence as the model wrote it (the VAT phrase in it fails the deterministic VAT check and is
    cut) and answers ``final_reply`` for the sentence as saved."""

    def rule(text, input, n):
        return "supported" if "מע" in text else final_reply

    return Judge(rule)


@pytest.mark.parametrize("reply", [
    {"verdict": "partial", "defect": "units", "reason": "המע\"מ אינו מצוין"},
    {"verdict": "partial", "defect": "none", "claims": [
        {"span": "9,500 ₪", "verdict": "partial"}]},
    {"verdict": "partial", "defect": "units", "claims": [
        {"span": "9,500 ₪", "verdict": "unsupported", "failure": "wrong_unit"}]},
], ids=["units-defect", "partial-claim", "wrong-unit-claim"])
def test_a_qualifier_edited_sentence_judged_partial_for_the_attribute_only_stays_marked_partly_verified(offline, reply):
    offline.texts = [VAT_SOURCE]
    out = _turn(ScriptedAgent([SEARCH, final(VAT_CLAIM), final(VAT_CLAIM), final(VAT_CLAIM)], judge=_edited_then(reply)),
                "מה השווי?")
    md = out.answer.answer_markdown
    assert "9,500" in md and "אומת חלקית" in md and "מע\"מ" not in md.split("\n")[0], md
    assert out.report.removals() == [] and out.report.counts()["edited"] == 1
    assert any(p.check == "final" and p.reason == final_check.FINAL_PARTIAL for p in out.report.problems)
    assert _purposes(out).count("verify_final") == 1 and out.final_digest == text_digest(md)


@pytest.mark.parametrize("reply", [
    {"verdict": "partial", "defect": "part", "claims": [
        {"span": "9,500 ₪", "verdict": "unsupported", "failure": "absent_from_source"}]},
    {"verdict": "partial", "defect": "formula"},
    {"verdict": "partial", "defect": "units", "claims": [
        {"span": "9,500 ₪", "verdict": "contradicts"}]},
], ids=["wrong-number", "other-defect", "contradicted-claim"])
def test_a_qualifier_edited_sentence_judged_partial_for_another_defect_is_handled_as_before(offline, reply):
    offline.texts = [VAT_SOURCE]
    out = _turn(ScriptedAgent([SEARCH, final(VAT_CLAIM), final(VAT_CLAIM), final(VAT_CLAIM)], judge=_edited_then(reply)),
                "מה השווי?")
    assert "9,500" not in out.answer.answer_markdown.split("\n\n")[0], out.answer.answer_markdown
    (p,) = out.report.removals()
    assert p.check == "final"


def test_a_sentence_no_qualifier_edit_touched_judged_partial_for_an_attribute_is_still_removed(offline):
    """The rule is for the qualifier-edited sentence only: a sentence whose claim span was cut (no qualifier edit) and that the
    final judge calls partly supported for its units keeps today's path, removal."""
    out = _turn(ScriptedAgent([SEARCH, final(f"{MIXED}\n{VALUE}")], judge=_as_saved({"verdict": "partial", "defect": "units"})))
    md = out.answer.answer_markdown
    assert out.report.counts()["edited"] == 0 and "1,200" not in md and "9,500" in md
    (p,) = out.report.removals()
    assert p.check == "final" and p.failure_kind == "wrong_unit"


def test_a_cut_sentence_supported_by_a_source_it_does_not_cite_gets_the_citation_attached(offline):
    offline.texts = [SOURCE, "נספח: השטח העיקרי בתא שטח 1 הוא 1,200 מ״ר."]
    uncited = f"{AREA}, {CLAUSE}."

    def rule(text, input, n):
        if "<server>" in input:  # as saved: supported by the second source, which the sentence does not cite
            return {"verdict": "supported", "supported_by": ["S2"]}
        return {"verdict": "partial", "defect": "part", "claims": [
            {"span": AREA, "verdict": "supported"}, {"span": CLAUSE, "verdict": "unsupported",
                                                     "failure": "absent_from_source"}]}

    out = _turn(ScriptedAgent([SEARCH, final(uncited)], judge=Judge(rule)))
    md = out.answer.answer_markdown
    assert "1,200" in md and MARKER in md and "[S2]" in md, md
    assert _kinds(out).count("citation") == 1 and out.report.removals() == []
    attached = [p for p in out.report.problems if p.check == "final" and p.kind == "needs_citation"]
    assert [(p.cite, p.reason) for p in attached] == [("S2", final_check.FINAL_ATTACH.format(source="S2"))]
    assert out.final_digest == text_digest(md)


def test_support_named_in_a_source_the_sentence_does_not_cite_is_refused_when_the_source_does_not_state_its_number(offline):
    offline.texts = [SOURCE, "נספח: השטח העיקרי בתא שטח 1 הוא 900 מ״ר."]

    def rule(text, input, n):
        if "<server>" in input:
            return {"verdict": "supported", "supported_by": ["S2"]}
        return {"verdict": "partial", "defect": "part", "claims": [
            {"span": AREA, "verdict": "supported"}, {"span": CLAUSE, "verdict": "unsupported",
                                                     "failure": "absent_from_source"}]}

    out = _turn(ScriptedAgent([SEARCH, final(f"{AREA}, {CLAUSE}.\n{VALUE}")], judge=Judge(rule)))
    md = out.answer.answer_markdown
    assert "1,200" not in md and "9,500" in md and "[S2]" not in md, md
    (p,) = out.report.removals()
    assert (p.check, p.failure_kind, p.cause) == ("final", "absent_from_source", "")
    assert p.reason.startswith(final_check.FINAL_UNSUPPORTED.format(reason="")) and "S2" in p.reason, p.reason
    assert out.final_digest == text_digest(md)


# --- round 13 U11 (KTD13, R16, AE7): the final text reads whole after removals -------------------------------------------

LEAD = "הנתונים לחלקה 7 הם:"
BULLET_A = "- מקדם א הוא 0.8 [S1]"
BULLET_B = "- מקדם ב הוא 0.9 [S1]"
BACK = "שני המקדמים לעיל חלים על כל החלקה [S1]."
COEFFICIENTS = "חלקה 7: מקדם א 0.8, מקדם ב 0.9. שני המקדמים חלים על כל החלקה."
COEFFICIENT_QUESTION = "מה המקדמים בחלקה 7?"


def _coefficient_rule(removed: bool):
    """Verification: the two bullets are unsupported (``removed``) or supported. The final check, shown the sentence that
    refers back with its removed predecessors (``removed_before``), names them in ``depends_on``."""
    def rule(text, input, n):
        if "מקדם" in text and "לעיל" not in text and "שני" not in text:
            return "unsupported" if removed else "supported"
        if "לעיל" in text and 'removed_before="' in input:
            ids = re.search(r'<preceding unit="\d+" removed_before="([\d,]+)"', input).group(1)
            return {"verdict": "supported", "depends_on": [int(i) for i in ids.split(",")]}
        return "supported"

    return rule


def _coefficient_turn(offline, rule, answer: str | None = None):
    offline.texts = [COEFFICIENTS]
    answer = answer or f"{LEAD}\n{BULLET_A}\n{BULLET_B}\n{BACK}"
    judge = Judge(rule)
    return _turn(ScriptedAgent([SEARCH, final(answer), final(answer), final(answer)], judge=judge),
                 COEFFICIENT_QUESTION), judge


def test_ae7_when_both_bullets_are_removed_the_lead_in_goes_and_the_sentence_that_refers_back_is_removed_as_dependent(offline):
    out, judge = _coefficient_turn(offline, _coefficient_rule(True))
    md = out.answer.answer_markdown
    assert LEAD not in md and "לעיל" not in md and "מקדם" not in md.split("\n\n> ")[0], md
    removals = {p.unit.index: p for p in out.report.removals()}
    assert set(removals) == {0, 1, 2, 3}
    assert removals[0].check == "dependency" and removals[3].check == "dependency"
    assert removals[3].failure_kind == removals[1].failure_kind and removals[3].basis
    # the sentence was judged again, as saved, with the removed units beside it and the text that stays before it
    final_input = judge.inputs[-1]
    assert '<preceding unit="3" removed_before="0,1,2">' in final_input and "<removed_units>" in final_input
    assert _purposes(out).count("verify_final") == 1
    assert out.final_digest == text_digest(md)


def test_ae7_when_the_bullets_stay_both_sentences_stay_and_nothing_is_judged_again(offline):
    out, _ = _coefficient_turn(offline, _coefficient_rule(False))
    md = out.answer.answer_markdown
    assert md == f"{LEAD}\n{BULLET_A}\n{BULLET_B}\n{BACK}"
    assert "verify_final" not in _purposes(out) and out.report.removals() == []


def test_a_back_reference_with_no_removed_predecessor_is_not_judged_again(offline):
    """"כאמור" before a unit that was removed is no orphan: the sentence's key is its draft key and its verdict is reused."""
    offline.texts = [SOURCE]
    answer = "השווי למ\"ר הוא 9,500 ₪ [S1].\nכאמור, הנכס פנוי [S1].\nהשטח המותר הוא 77 מ״ר [S1]."

    def rule(text, input, n):
        return {"verdict": "unsupported", "failure": "absent_from_source"} if "77" in text else "supported"

    out = _turn(ScriptedAgent([SEARCH, final(answer), final(answer), final(answer)], judge=Judge(rule)),
                "מה השווי?")
    md = out.answer.answer_markdown
    assert "כאמור" in md and "77" not in md
    assert "verify_final" not in _purposes(out) and out.final_digest == text_digest(md)


def test_a_back_reference_sentence_is_judged_again_only_in_the_section_the_removal_was_in(offline):
    offline.texts = [SOURCE]
    answer = ("## א\nהשטח המותר הוא 77 מ״ר [S1].\n## ב\nהשווי למ\"ר הוא 9,500 ₪ [S1].\nכאמור, הנכס פנוי [S1].")

    def rule(text, input, n):
        return {"verdict": "unsupported", "failure": "absent_from_source"} if "77" in text else "supported"

    out = _turn(ScriptedAgent([SEARCH, final(answer), final(answer), final(answer)], judge=Judge(rule)), "מה השווי?")
    assert "כאמור" in out.answer.answer_markdown and "verify_final" not in _purposes(out)


def test_a_back_reference_the_judge_finds_independent_stays(offline):
    out, judge = _coefficient_turn(
        offline, lambda t, i, n: "unsupported" if "מקדם ב" in t and "לעיל" not in t else "supported")
    shown = re.search(r'<preceding unit="3" removed_before="2">(.*?)</preceding>', judge.inputs[-1], re.S).group(1)
    assert "מקדם א הוא 0.8" in shown and "מקדם ב" not in shown  # what stays before it, not what was removed
    md = out.answer.answer_markdown
    assert BULLET_A in md and BACK in md and "מקדם ב" not in md.split("\n\n> ")[0].replace(BACK, "")
    assert _purposes(out).count("verify_final") == 1  # judged again: a predecessor was removed
    assert out.report.removed_units() == {2}


def test_without_time_a_back_reference_after_a_removal_is_withheld_not_kept_unchecked(offline, monkeypatch):
    monkeypatch.setattr(final_check, "FINAL_MIN_SECONDS", 10**6)
    out, _ = _coefficient_turn(offline, _coefficient_rule(True))
    md = out.answer.answer_markdown
    assert "verify_final" not in _purposes(out) and "לעיל" not in md
    sentence = next(p for p in out.report.removals() if p.unit.index == 3)
    assert sentence.failure_kind == "not_checked" and sentence.cause == "time_limit"
    assert out.final_digest == text_digest(md)


def test_a_lead_in_whose_items_the_final_check_removed_goes_with_them(offline):
    """U10 removed a lead-in whose items verification removed; the same when the FINAL check removed them (R16)."""
    offline.texts = [f"{COEFFICIENTS} השווי למ\"ר הוא 9,500 ₪."]
    items = f"- מקדם א הוא 0.8, {CLAUSE} [S1]\n- מקדם ב הוא 0.9, {CLAUSE} [S1]"

    def rule(text, input, n):
        if n == 1 and CLAUSE in text:
            return {"verdict": "partial", "defect": "part", "claims": [
                {"span": text.replace(f", {CLAUSE}", ""), "verdict": "supported"},
                {"span": CLAUSE, "verdict": "unsupported", "failure": "absent_from_source"}]}
        return {"verdict": "unsupported", "failure": "absent_from_source"} if MARKER in text else "supported"

    answer = f"{LEAD}\n{items}\n{VALUE}"
    out = _turn(ScriptedAgent([SEARCH, final(answer), final(answer), final(answer)], judge=Judge(rule)),
                COEFFICIENT_QUESTION)
    md = out.answer.answer_markdown
    assert md.startswith(VALUE) and LEAD not in md and "מקדם" not in md.split("\n\n> ")[0]
    lead = next(p for p in out.report.removals() if p.unit.index == 0)
    assert lead.check == "dependency" and lead.failure_kind == "absent_from_source" and lead.basis
    assert {p.check for p in out.report.removals() if p.unit.index in (1, 2)} == {"final"}
    assert out.final_digest == text_digest(md)


def test_a_first_line_a_cut_would_leave_without_a_word_is_removed_whole_and_the_saved_text_opens_with_substance(offline):
    """KTD13 rule 1: "הפחתה של" fails and only "20%" would stand beside a marker. The unit goes whole, with no marker, as a
    dependency; the registry has the removal note only, and the text recomposes to the byte (the fixed point)."""
    offline.texts = ["מקדם הפחתה 20% נקבע לחלקה. " + SOURCE]
    stub = "הפחתה של"

    def rule(text, input, n):
        if n == 1 and stub in text:
            return {"verdict": "partial", "defect": "part", "claims": [
                {"span": stub, "verdict": "unsupported", "failure": "absent_from_source"},
                {"span": "20%", "verdict": "supported"}]}
        return "supported"

    answer = f"{stub} 20% [S1].\n{VALUE}"
    out = _turn(ScriptedAgent([SEARCH, final(answer), final(answer), final(answer)], judge=Judge(rule)), "מה ההפחתה?")
    md = out.answer.answer_markdown
    assert md.startswith(VALUE) and MARKER not in md and "20%" not in md.split("\n\n> ")[0]
    (p,) = out.report.removals()
    assert p.check == "dependency" and p.unit.index == 0 and p.failure_kind == "absent_from_source"
    assert _kinds(out) == ["removal_note"] and out.final_digest == text_digest(md)


def test_the_servers_coverage_note_never_reaches_the_judge_as_a_claim(offline):
    """Round 15 U7 (KTD8, R11): the note the server appends after the verification is not in any judge call of the turn, and
    the final check of the saved text judges nothing of it — it is a registered server span, checked by construction."""
    from app.chat import timeframe as TF

    note = TF.MSG_NOTE_RECENT + "\n" + TF.MSG_GROUP_CAP + ": שומה סינתטית."
    offline.coverage = lambda answer: note
    judge = Judge(lambda t, i, n: "supported")
    out = _turn(ScriptedAgent([SEARCH, final(VALUE), final(VALUE), final(VALUE)], judge=judge))
    md = out.answer.answer_markdown
    assert TF.LATEST_CHECKED in md and _kinds(out) == ["coverage_note"], (md, _kinds(out))
    assert judge.inputs, "the model's own sentence was judged"
    assert all(TF.LATEST_CHECKED not in i and TF.MSG_GROUP_CAP not in i for i in judge.inputs), "the note was never shown"
    assert out.final_check["judge_calls"] == 0


# --- round 17 U1: the final check admits an untouched score only while its component's evidence is unchanged (KTD1) -------------

def _scored_draft(monkeypatch):
    """The two-component draft verified once (N1 given by the three rate units, N2 by the value unit), and the judge's scripts."""
    from app.chat import coverage
    from tests.unit import test_coverage as tc

    monkeypatch.setattr(coverage, "build", lambda ws, answer, question: ({}, answer))  # no lookups: no database

    ws, turn = tc._rates_file(), tc._two_components()
    score = tc._gives({"N1": ("שיעור היוון", "full"), "N2": (tc.ASK_VALUE, "full")})
    answer = tc._answer(tc.V1_MD)
    report = verify.verify_answer(tc._judge(score), answer, ws, "?", [], requirements=turn, item_version=1)
    return tc, ws, turn, score, answer, report


def _read_one_more_passage(tc, ws) -> None:
    s2 = ws.sources["S2"]
    extra = ws.add_source(document_id=s2.document_id, version_id=s2.version_id, title="דוח בדיקה", section="נספח",
                          location="נספח", kind="context", text="שיעורי ההיוון שנקבעו לנכסים נוספים: מחסנים: 9.1%")
    extra.contexts, extra.unit = list(s2.contexts), s2.unit
    tc._read(ws, extra.sid)


def test_the_final_check_leaves_an_untouched_score_alone_while_the_evidence_of_its_component_is_unchanged(monkeypatch):
    tc, ws, turn, score, answer, report = _scored_draft(monkeypatch)
    seen: list[str] = []
    usage: list = []
    done = final_check.finalize(tc._judge(score, seen=seen), ws, answer, report, turn, question="?", usage=usage)
    assert seen == [] and usage == []  # nothing changed: no judge call at all
    assert [(o["id"], o["status"]) for o in report.requirement_outcomes()] == [("N1", "full"), ("N2", "full")]
    assert done.composition.answer.answer_markdown.startswith(tc.V1_MD)


def test_the_final_check_judges_again_the_giving_units_of_a_score_whose_component_gained_a_passage_and_only_those(monkeypatch):
    tc, ws, turn, score, answer, report = _scored_draft(monkeypatch)
    _read_one_more_passage(tc, ws)  # read after the verification, bearing on N1 only
    seen: list[str] = []
    usage: list = []

    def late(frozen, units):  # the judge, shown the passage, now finds the rates incomplete
        return [tc._req("", "partial" if cid == "N1" else "full", list(units), id=cid) | ({"missing": "שיעורי ההיוון"} if cid == "N1" else {})
                for cid, _ in frozen if any(("שיעור היוון" if cid == "N1" else tc.ASK_VALUE) in t for t in units.values())]

    final_check.finalize(tc._judge(late, seen=seen), ws, answer, report, turn, question="?", usage=usage)
    assert len(seen) == 1 and [u["purpose"] for u in usage] == [final_check.FINAL_LABEL]  # one call, the final check's own
    assert set(tc._units(seen[0])) == {0, 1, 2} and "<requirements>" in seen[0]  # N1's giving units; N2's unit was not judged again
    outcomes = {o["id"]: o["status"] for o in report.requirement_outcomes()}
    assert outcomes == {"N1": "partial", "N2": "full"}  # decided from the fresh score; the old full was replaced
    n1, n2 = report.requirement_decisions["N1"], report.requirement_decisions["N2"]
    assert [(x["origin"], x["version"]) for x in n1["rests_on"]] == [("fresh", 1)] and n1["reused"] is False
    assert report.requirement_votes["N1"][0]._stamp["final"] is True and "final" not in report.requirement_votes["N2"][0]._stamp
    assert n2["rests_on"][0]["origin"] == "fresh" and n2["status"] == "full"


def test_the_final_check_without_time_to_judge_keeps_the_drafts_scores_even_when_their_evidence_changed(monkeypatch):
    tc, ws, turn, score, answer, report = _scored_draft(monkeypatch)
    _read_one_more_passage(tc, ws)
    seen: list[str] = []
    done = final_check.finalize(tc._judge(score, seen=seen), ws, answer, report, turn, question="?", usage=[],
                                deadline=time.monotonic() - 1)
    assert seen == []  # no allowance left: the draft's verdicts stand, nothing is withheld for a changed passage
    assert done.composition.answer.answer_markdown.startswith(tc.V1_MD)
    assert [o["status"] for o in report.requirement_outcomes()] == ["full", "full"]


# --- a scope the sentence omits is added, never a reason to remove it (round 17 U5, KTD5, R10, R11; D07 run 2) ------------------------

from tests.unit.test_verify_edit_ladder import (  # noqa: E402
    SCOPE,
    SCOPE_ADDED,
    SCOPE_UNIT,
    _table,
)

SCOPE_CUT = f"בשיעור היוון 6.0% שווי הנכס הוא 11,800 אלפי ₪, {CLAUSE} [S1]."  # the clause the source does not state is cut


def _scope_rule(final_reply):
    """The first verification cuts the clause the source does not state; the sentence as saved (the marker in it) is answered
    ``final_reply`` — as the real judge answered the cut sentence for lacking the limitation (D07 run 2)."""

    def rule(text, input, n):
        if n == 1 and CLAUSE in text:
            return {"verdict": "partial", "defect": "part", "claims": [
                {"span": "בשיעור היוון 6.0% שווי הנכס הוא 11,800 אלפי ₪", "verdict": "supported"},
                {"span": CLAUSE, "verdict": "unsupported", "failure": "absent_from_source"}]}
        return final_reply if MARKER in text else "supported"

    return rule


def _scope_turn(offline, final_reply, source: str | None = None):
    offline.texts = [source or _table()]
    return _turn(ScriptedAgent([SEARCH, final(SCOPE_CUT), final(SCOPE_CUT), final(SCOPE_CUT)],
                               judge=Judge(_scope_rule(final_reply))), "מהו שווי הנכס בשיעור היוון של 6.0%?")


def test_a_cut_sentence_the_final_judge_finds_wider_than_its_scope_is_qualified_with_the_validated_phrase_not_removed(offline):
    out = _scope_turn(offline, {"verdict": "partial", "defect": "scope", "scope": SCOPE})
    md = out.answer.answer_markdown
    first = md.split("\n")[0]
    assert first.startswith(f"בשיעור היוון 6.0% שווי הנכס הוא 11,800 אלפי ₪ {SCOPE_ADDED}") and "[S1]" in first, md
    assert MARKER in first and out.report.removals() == [] and "הוסרו" not in md
    assert _kinds(out) == ["qualifier", "marker"], _kinds(out)
    assert _purposes(out).count("verify_final") == 1 and out.final_check["passes"] == 2  # recomposed once, nothing judged again
    assert out.final_digest == text_digest(md)  # (the fixed point is asserted by the check itself)
    assert {r.post_check for r in out.report.claims[0] if r.verdict == "supported"} == {"partial"}


def test_a_scope_the_final_judge_names_that_the_source_does_not_state_leaves_the_value_with_a_limitation_note(offline):
    out = _scope_turn(offline, {"verdict": "partial", "defect": "scope", "scope": "רק בתרחיש הפיתוח המלא"})
    md = out.answer.answer_markdown
    first = md.split("\n")[0]
    assert "11,800" in first and "(היקף הערך במקור לא אומת; הערך נקרא ב-S1 (סעיף \"סיכום\"))" in first, md  # the fixture's location
    assert "תרחיש" not in md and out.report.removals() == [] and _kinds(out) == ["scope_note", "marker"]
    assert out.final_digest == text_digest(md)


def test_a_final_verdict_unsupported_for_the_scope_alone_is_taken_as_partly_supported_and_qualified(offline):
    out = _scope_turn(offline, {"verdict": "unsupported", "failure": "absent_from_source", "defect": "scope", "scope": SCOPE})
    md = out.answer.answer_markdown
    assert SCOPE_ADDED in md and "11,800" in md and out.report.removals() == []


def test_the_scope_is_not_added_twice_when_verification_already_wrote_it(offline):
    """The first judge names the scope on the sentence as the model wrote it; the final judge names it again on the cut one."""

    def rule(text, input, n):
        if n == 1 and CLAUSE in text:
            return {"verdict": "partial", "defect": "scope", "scope": SCOPE, "claims": [
                {"span": "בשיעור היוון 6.0% שווי הנכס הוא 11,800 אלפי ₪", "verdict": "supported"},
                {"span": CLAUSE, "verdict": "unsupported", "failure": "absent_from_source"}]}
        return {"verdict": "partial", "defect": "scope", "scope": SCOPE} if MARKER in text else "supported"

    offline.texts = [_table()]
    out = _turn(ScriptedAgent([SEARCH, final(SCOPE_CUT), final(SCOPE_CUT), final(SCOPE_CUT)], judge=Judge(rule)),
                "מהו שווי הנכס בשיעור היוון של 6.0%?")
    assert out.answer.answer_markdown.count(SCOPE) == 1 and out.report.removals() == []


def test_a_scope_defect_with_no_cited_source_marks_the_sentence_partly_verified_and_never_removes_it():
    """``attribute_only``: a unit that cites no source has nowhere to say where it was read, so the server writes nothing; the
    sentence is kept, marked."""
    from types import SimpleNamespace

    final = object.__new__(final_check._Final)
    unit = SimpleNamespace(drafts=[0])
    scope = verify.JudgeVerdict(index=0, verdict="partial", reason="x", defect="scope", scope=SCOPE)
    assert final.attribute_only(unit, scope, []) is True
    assert final.attribute_only(unit, scope, [SimpleNamespace(verdict="partial")]) is True
    assert final.attribute_only(unit, scope, [SimpleNamespace(verdict="unsupported")]) is False  # another reason keeps the removal


def test_a_scope_defect_never_maps_to_a_failure_that_removes():
    assert final_check.DEFECT_FAILURES["scope"] == "context_not_read"
    assert SCOPE_UNIT  # (the sentence the sequences use)
