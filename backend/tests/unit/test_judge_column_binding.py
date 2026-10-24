"""Round 23 U4 (KTD8, R5, R16): the judge reads a page-text row whose columns could not be told with a neutral ``<column_binding>`` fact, and a
failure that rests only on a column or cell mismatch of such a row is a mark, never a cut. The judge here is scripted; these tests prove the
server's handling, not the model's judgement. Synthetic text only: every name and amount is invented."""

from __future__ import annotations

import re
import uuid

import pytest

from app.chat import tools, verify
from app.chat.engine import FinalAnswer
from app.chat.tools import Workspace
from app.chat.verify import verify_answer
from tests.support.scripted_provider import ScriptedProvider

ROW = "מחסן צפוני 410 1,730 2,200 (הנתון של השורה)"
CLAIM = "דמי השכירות לשנה השנייה הם 2,200 ₪ [S1]."


def _ws(text: str = ROW, *, unbound: bool = False, block: int = 12, kind: str = "context", unknown_at: int | None = None) -> Workspace:
    ws = Workspace(ctx=None)
    vid = uuid.uuid4()
    src = ws.add_source(document_id=uuid.uuid4(), version_id=vid, title="דוח בדיקה", section="טבלת השכירות", location="עמוד 3",
                        kind=kind, text=text)
    src.block_start = src.block_end = block
    if unbound:
        ws.column_unknown.add((str(vid), block if unknown_at is None else unknown_at))
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


@pytest.mark.parametrize("verdict, failure", [("unsupported", "absent_from_source"), ("contradicts", "contradicts_source"),
                                              ("unsupported", "wrong_unit")])
@pytest.mark.parametrize("reason", ["המספר 2,200 כתוב בעמודה אחרת מזו שהטענה מייחסת לו", "התא של השנה השנייה מכיל מספר אחר",
                                    "the figure sits in another column of that row"])
def test_a_column_mismatch_failure_of_an_unbound_row_is_a_mark_not_a_cut(verdict, failure, reason):
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
