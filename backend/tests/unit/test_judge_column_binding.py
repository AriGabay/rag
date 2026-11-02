"""Round 23 U4 (KTD8, R5, R16): the judge reads a page-text row whose columns could not be told with a neutral ``<column_binding>`` fact, and a
failure that rests only on a column or cell mismatch of such a row is a mark, never a cut. The judge here is scripted; these tests prove the
server's handling, not the model's judgement. Synthetic text only: every name and amount is invented."""

from __future__ import annotations

import re
import uuid

import pytest

from app.chat import columns, tools, verify
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import verify_answer
from tests.support.scripted_provider import ScriptedProvider

ROW = "מחסן צפוני 410 1,730 2,200 (הנתון של השורה)"
CLAIM = "דמי השכירות לשנה השנייה הם 2,200 ₪ [S1]."


BOUND_ROW = "מחסן דרומי 380 1,540 1,900 (שורה אחרת)"


def _ws(text: str = ROW, *, unbound: bool = False, block: int = 12, kind: str = "context", unknown_at: int | None = None,
        recorded: bool = True, bound_row: str = "") -> Workspace:
    """``recorded``: the unbound row's text is in ``ws.column_bindings`` as ``_bound_rows`` stores it; ``bound_row``: a second row of the source
    (the next block) whose columns WERE told."""
    ws = Workspace(ctx=None)
    if bound_row:
        text = f"{text}\n{bound_row}"
    vid = uuid.uuid4()
    src = ws.add_source(document_id=uuid.uuid4(), version_id=vid, title="דוח בדיקה", section="טבלת השכירות", location="עמוד 3",
                        kind=kind, text=text)
    src.block_start = src.block_end = block
    if bound_row:
        src.block_end = block + 1
        ws.column_bindings[(str(vid), block + 1, bound_row)] = columns.Binding(columns.BOUND)
    if unbound:
        ws.column_unknown.add((str(vid), block if unknown_at is None else unknown_at))
        if recorded:
            ws.column_bindings[(str(vid), block if unknown_at is None else unknown_at, ROW)] = columns.Binding(columns.UNKNOWN, "no word positions")
    return ws


def _answer(markdown: str) -> FinalAnswer:
    return FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
                       missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                       focus=None, requested=[], parts=[])


def _judge(verdict: str, reason: str, failure: str = "none") -> ScriptedProvider:
    p = ScriptedProvider()
    p.seen = []

    def respond(instructions: str, input: str) -> dict:
        p.seen.append(input)
        return {"verdicts": [{"index": int(m.group(1)), "verdict": verdict, "reason": reason, "failure": failure}
                             for m in re.finditer(r'<unit index="(\d+)"', input)]}

    from app.providers.llm import Purpose

    p.on(Purpose.VERIFY, respond, repeat=True)
    return p


def _render(ws: Workspace) -> str:
    return verify._render_batch(verify._Batch(verify.split_units(CLAIM)), ws)


def test_the_judge_source_block_carries_the_column_binding_note_of_an_unbound_page_text_row():
    out = _render(_ws(unbound=True))
    m = re.search(r'<source id="S1".*?</source>', out, re.S)
    assert m and f"<column_binding>{tools.MSG_COLUMNS_UNKNOWN}</column_binding>" in m.group(0)
    assert out.count("<column_binding>") == 1


@pytest.mark.parametrize("block", [3, 12, 30])
def test_a_bound_row_a_row_elsewhere_and_a_table_source_carry_no_note(block):
    assert "<column_binding>" not in _render(_ws(unbound=False, block=block))  # bound (or never unknown)
    assert "<column_binding>" not in _render(_ws(unbound=True, block=block, unknown_at=block + 50))  # an unbound row outside the source's blocks
    table = _ws("| שנה | סכום |\n|---|---|\n| שנייה | 2,200 |", unbound=False, block=block, kind="table")
    assert "<column_binding>" not in _render(table)


def test_the_policy_rule_and_the_contract_version():
    assert "<column_binding>" in verify.JUDGE_POLICY and verify.COLUMN_UNKNOWN_MARK in verify.JUDGE_POLICY
    assert verify.JUDGE_VERSION == 23


COLUMN_REASONS = ["המספר 2,200 כתוב בעמודה אחרת מזו שהטענה מייחסת לו", "התא של השנה השנייה מכיל מספר אחר",
                  "the figure sits in another column of that row"]
BOUND_CLAIM = "דמי השכירות לשנה השנייה במחסן הדרומי הם 1,900 ₪ [S1]."


def _marked(r) -> bool:
    return r.removed_units() == set() and [p.check for p in r.problems if p.severity == "partial"] == [verify.COLUMN_UNKNOWN_CHECK]


@pytest.mark.parametrize("verdict, failure", [("unsupported", "wrong_unit")])  # (an unsupported claim is never failure "none": it is normalised to absent_from_source)
@pytest.mark.parametrize("reason", COLUMN_REASONS)
def test_a_column_mismatch_failure_of_an_unbound_row_is_a_mark_not_a_cut(verdict, failure, reason):
    """The regex fallback (the reason names a column or cell, no lead marker) converts a failure that is ``none`` or ``wrong_unit`` when the
    claim's number sits in the row recorded as unbound (round 23 review #1)."""
    ws = _ws(unbound=True)
    a = _answer(CLAIM)
    r = verify_answer(_judge(verdict, reason, failure), a, ws, "?", [])
    assert r.removed_units() == set(), r.problems
    (rec,) = r.claims[0]
    assert rec.verdict == "partial" and r.verdicts[0] == "partial"
    marks = [p for p in r.problems if p.severity == "partial"]
    assert [p.check for p in marks] == [verify.COLUMN_UNKNOWN_CHECK] and not any(p.repairable for p in r.problems)
    assert verify.partial_reason_class(None, verify.COLUMN_UNKNOWN_CHECK) == "columns_unknown"
    assert "2,200 ₪" in r.apply(a).answer_markdown


@pytest.mark.parametrize("verdict, failure", [("unsupported", "absent_from_source"), ("contradicts", "contradicts_source")])
@pytest.mark.parametrize("reason", COLUMN_REASONS)
def test_a_reason_that_merely_mentions_a_column_never_turns_an_absent_or_contradicted_number_into_a_mark(verdict, failure, reason):
    """A regex hit alone is never enough for ``contradicts_source`` / ``absent_from_source``: the source states another number, the claim goes."""
    r = verify_answer(_judge(verdict, reason, failure), _answer(CLAIM), _ws(unbound=True), "?", [])
    assert 0 in r.removed_units()
    r2 = verify_answer(_judge(verdict, reason, failure), _answer("דמי השכירות לשנה השנייה הם 2,300 ₪ [S1]."), _ws(unbound=True), "?", [])
    assert 0 in r2.removed_units()  # the source states 2,200, not 2,300


@pytest.mark.parametrize("verdict, failure", [("unsupported", "absent_from_source"), ("contradicts", "contradicts_source"), ("unsupported", "wrong_unit")])
def test_the_judge_leading_with_the_marker_converts_any_confirmable_failure_on_the_unbound_row(verdict, failure):
    a = _answer(CLAIM)
    r = verify_answer(_judge(verdict, verify.COLUMN_UNKNOWN_MARK + ": המספר כתוב בשורה, אך לא נקבע תחת איזו עמודה", failure), a, _ws(unbound=True), "?", [])
    assert _marked(r), r.problems
    assert "2,200 ₪" in r.apply(a).answer_markdown


@pytest.mark.parametrize("reason", COLUMN_REASONS)
def test_the_regex_fallback_on_a_bound_row_of_a_source_that_also_holds_an_unbound_row_is_a_cut(reason):
    """The source's block range holds an unbound row (so the source carries the note) but the claim's number is in the bound row: removed."""
    ws = _ws(unbound=True, bound_row=BOUND_CLAIM and BOUND_ROW)
    r = verify_answer(_judge("unsupported", reason, "wrong_unit"), _answer(BOUND_CLAIM), ws, "?", [])
    assert 0 in r.removed_units()
    led = verify.COLUMN_UNKNOWN_MARK + ": המספר כתוב בשורה, אך לא נקבע תחת איזו עמודה"
    r2 = verify_answer(_judge("unsupported", led, "wrong_unit"), _answer(BOUND_CLAIM), _ws(unbound=True, bound_row=BOUND_ROW), "?", [])
    assert 0 in r2.removed_units()  # the marker is no licence either when the number is located in a bound row
    # the same two-row source: the unbound row's own number is still a mark
    ok = verify_answer(_judge("unsupported", reason, "wrong_unit"), _answer(CLAIM), _ws(unbound=True, bound_row=BOUND_ROW), "?", [])
    assert _marked(ok), ok.problems


def test_without_the_row_records_the_regex_fallback_is_a_cut_and_only_the_led_marker_converts():
    """(b) cannot be decided when ``ws.column_bindings`` holds no text of the unbound row: then (a) must be the judge's led marker."""
    reason = "המספר 2,200 כתוב בעמודה אחרת מזו שהטענה מייחסת לו"
    r = verify_answer(_judge("unsupported", reason, "wrong_unit"), _answer(CLAIM), _ws(unbound=True, recorded=False), "?", [])
    assert 0 in r.removed_units()
    led = verify_answer(_judge("unsupported", verify.COLUMN_UNKNOWN_MARK + ": " + reason, "wrong_unit"), _answer(CLAIM),
                        _ws(unbound=True, recorded=False), "?", [])
    assert _marked(led), led.problems


@pytest.mark.parametrize("verdict, failure", [("unsupported", "absent_from_source"), ("contradicts", "contradicts_source")])
def test_the_same_verdict_on_a_bound_row_is_a_cut_as_usual(verdict, failure):
    ws = _ws(unbound=False)
    r = verify_answer(_judge(verdict, "המספר 2,200 כתוב בעמודה אחרת מזו שהטענה מייחסת לו", failure), _answer(CLAIM), ws, "?", [])
    assert 0 in r.removed_units()


@pytest.mark.parametrize("reason", ["המקור אומר שהנכס פנוי, והטענה אומרת שהוא מושכר", "תא שטח 2 אינו הנכס שנשאל עליו",
                                    "התקופה שנכתבה במפורש היא שנה ראשונה"])
def test_a_failure_that_does_not_rest_on_a_column_stays_a_cut_even_for_an_unbound_row(reason):
    r = verify_answer(_judge("unsupported", reason, "absent_from_source"), _answer(CLAIM), _ws(unbound=True), "?", [])
    assert 0 in r.removed_units()


def test_the_judge_following_rule_36_gets_the_dedicated_mark():
    reason = verify.COLUMN_UNKNOWN_MARK + ": המספר כתוב בשורה, אך לא נקבע תחת איזו עמודה"
    a = _answer(CLAIM)
    r = verify_answer(_judge("partial", reason), a, _ws(unbound=True), "?", [])
    assert r.removed_units() == set()
    assert [p.check for p in r.problems if p.severity == "partial"] == [verify.COLUMN_UNKNOWN_CHECK]
    # the marker means nothing for a row whose columns were told: it is the judge's ordinary partial mark
    bound = verify_answer(_judge("partial", reason), a, _ws(unbound=False), "?", [])
    assert [p.check for p in bound.problems if p.severity == "partial"] != [verify.COLUMN_UNKNOWN_CHECK]
