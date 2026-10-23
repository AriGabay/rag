"""The turn's measurement fields of round 22 U0 (R10, R18, R25): the judge results that ended ``not_checked`` by cause, the
state of each read target (attempted, completed, failed with, skipped for the tool budget) and the number of multi-page
stored tables served — all in the stage clock's summary (``resolution["timing"]``), none of them read by a decision.
The judge and the tools are scripted; synthetic text only; no database."""

from __future__ import annotations

import json
import re
import uuid
from types import SimpleNamespace

from app.chat import tools as T
from app.chat import verify
from app.chat.engine import FinalAnswer
from app.chat.timing import StageClock
from app.chat.verify import verify_answer
from app.extraction import tables as stored_tables
from app.providers.llm import CallStatus, Purpose
from tests.support.scripted_provider import ScriptedProvider


def _ws() -> T.Workspace:
    return T.Workspace(ctx=None)


# --- judge results that ended not_checked, by cause ---------------------------------------------------------------------------

def _verify(respond, markdown: str, source: str) -> tuple[verify.VerifyReport, T.Workspace]:
    ws = _ws()
    ws.add_source(document_id=uuid.uuid4(), version_id=uuid.uuid4(), title="דוח בדיקה", section="סיכום",
                  location="סעיף \"סיכום\"", kind="context", text=source)
    answer = FinalAnswer(status="answered", answer_markdown=markdown, items=[], claims=[], clarification_question="",
                         missing_info="", referenced_document_ids=[], scope_kind="focused", scope_query="", omitted=[],
                         focus=None, requested=[], parts=[])
    p = ScriptedProvider()
    p.on(Purpose.VERIFY, lambda instructions, input: respond(input), repeat=True)
    return verify_answer(p, answer, ws, "?", []), ws


def _units(input: str) -> dict[int, str]:
    return {int(i): t for i, t in re.findall(r'<unit index="(\d+)" cites="[^"]*">\n(.*?)\n</unit>', input, re.S)}


def test_a_judge_batch_that_times_out_is_counted_as_not_checked_with_cause_timeout(monkeypatch):
    monkeypatch.setattr(verify, "JUDGE_MAX_UNITS", 1)

    def respond(input: str):
        texts = _units(input)
        if any("דמי השכירות הם 55" in t for t in texts.values()):
            return CallStatus.TIMEOUT
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "x"} for i in texts]}

    report, ws = _verify(respond, "השווי למ\"ר הוא 9,500 ₪ [S1].\nדמי השכירות הם 55 ₪ למ\"ר לחודש [S1].",
                         "סיכום: השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש.")
    assert [r.cause for r in report.removals()] == ["check_failed"]  # the answer's decision is the one it always was
    assert ws.clock.summary()["not_checked"] == {"timeout": 1}


def test_the_other_causes_are_counted_each_by_its_own_name(monkeypatch):
    monkeypatch.setattr(verify, "JUDGE_MAX_UNITS", 1)

    def respond(input: str):
        texts = _units(input)
        out = []
        for i, t in texts.items():
            if t.startswith("הנכס פנוי"):
                continue  # the judge leaves the unit out, twice
            out.append({"index": i, "verdict": "not_factual" if "כך נראה" in t else "supported", "reason": "x"})
        return {"verdicts": out}

    _, ws = _verify(respond, "השווי למ\"ר הוא 9,500 ₪ [S1].\nהנכס פנוי [S1].\nדמי השכירות 55 ₪ הם, כך נראה, נתון חשוב [S1].",
                    "סיכום: השווי למ\"ר הוא 9,500 ₪. דמי השכירות הם 55 ₪ למ\"ר לחודש.")
    assert ws.clock.summary()["not_checked"] == {"omitted": 1, "not_classified": 1}


def test_a_turn_with_every_claim_checked_counts_none(monkeypatch):
    def respond(input: str):
        return {"verdicts": [{"index": i, "verdict": "supported", "reason": "x"} for i in _units(input)]}

    _, ws = _verify(respond, "השווי למ\"ר הוא 9,500 ₪ [S1].", "סיכום: השווי למ\"ר הוא 9,500 ₪.")
    assert ws.clock.summary()["not_checked"] == {}


def test_the_final_checks_unchecked_sentence_is_counted_by_its_status():
    from app.chat.final_check import _Final

    ws = _ws()
    checker = _Final.__new__(_Final)
    checker.ws = ws
    checker.unchecked(SimpleNamespace(drafts=[0]), "check_failed", "rate_limited")
    checker.unchecked(SimpleNamespace(drafts=[0]), "time_limit", "time_limit")
    assert ws.clock.summary()["not_checked"] == {"rate_limited": 1, "time_limit": 1}


# --- the state of each read target -------------------------------------------------------------------------------------------

def _handlers(monkeypatch, **outputs):
    def make(out):
        def handler(ws, args):
            if isinstance(out, Exception):
                raise out
            return out
        return handler

    monkeypatch.setattr(T, "HANDLERS", {k: make(v) for k, v in outputs.items()})


def test_a_read_stopped_by_the_tool_budget_records_budget_skipped(monkeypatch):
    ws = _ws()
    ws.tool_budget = 10
    _handlers(monkeypatch, read="טקסט ארוך מהתקציב של הסבב")
    T.run_tool(ws, "read", json.dumps({"target": {"section": "§1"}}))  # reads, and spends the budget
    assert ws.budget_spent
    out = T.run_tool(ws, "read", json.dumps({"target": {"section": "§3"}}))
    assert out.startswith("<not_read")
    reads = ws.clock.summary()["reads"]
    skipped = next(v for k, v in reads.items() if "§3" in k)
    assert skipped == {"tool": "read", "attempted": False, "completed": False, "failed_with": None,
                       "budget_skipped": True, "calls": 1}
    done = next(v for k, v in reads.items() if "§1" in k)
    assert done["attempted"] and done["completed"] and not done["budget_skipped"] and done["failed_with"] is None


def test_a_read_the_server_refuses_records_what_it_failed_with(monkeypatch):
    ws = _ws()
    _handlers(monkeypatch, read=T.ToolError("אין", key="stale_handle"))
    T.run_tool(ws, "read", json.dumps({"target": {"table": "T2"}}))
    (state,) = ws.clock.summary()["reads"].values()
    assert state["attempted"] and not state["completed"] and state["failed_with"] == "stale_handle"
    assert not state["budget_skipped"]


def test_a_later_completed_read_of_the_same_target_clears_the_failure(monkeypatch):
    ws = _ws()
    _handlers(monkeypatch, read=T.ToolError("אין", key="refused"))
    T.run_tool(ws, "read", json.dumps({"target": {"table": "T2"}}))
    _handlers(monkeypatch, read="טבלה")
    T.run_tool(ws, "read", json.dumps({"target": {"table": "T2"}}))
    (state,) = ws.clock.summary()["reads"].values()
    assert state["completed"] and state["failed_with"] is None and state["calls"] == 2


def test_a_pages_target_is_named_by_its_document_and_range(monkeypatch):
    ws = _ws()
    _handlers(monkeypatch, read="טקסט")
    T.run_tool(ws, "read", json.dumps({"target": {"pages": {"document": "D1", "from_page": 3, "to_page": 5}}}))
    assert list(ws.clock.summary()["reads"]) == ["read:pages=D1:3-5"]


def test_a_tool_that_opens_no_target_records_no_read_state(monkeypatch):
    ws = _ws()
    _handlers(monkeypatch, search="תוצאה")
    T.run_tool(ws, "search", '{"query": "א"}')
    assert ws.clock.summary()["reads"] == {}


# --- multi-page stored tables served ---------------------------------------------------------------------------------------------

def _table(*pages: int | None) -> dict:
    return {"rows": [{"cells": ["א", str(i)], "page": p} for i, p in enumerate(pages)]}


def test_spans_pages_reads_the_stored_rows_pages_only():
    assert stored_tables.spans_pages(_table(4, 4, 5))
    assert not stored_tables.spans_pages(_table(4, 4, None))
    assert not stored_tables.spans_pages(_table())
    assert not stored_tables.spans_pages(None)


def test_a_multi_page_table_served_increments_the_counter_once(monkeypatch):
    version = uuid.uuid4()
    stored = {1: _table(7, 7, 8), 2: _table(9, 9)}
    monkeypatch.setattr(T.reader, "table_structure", lambda conn, v, i: stored.get(i))
    monkeypatch.setattr(T.reader, "table_block", lambda conn, v, i: None)
    ws = _ws()
    for index in (1, 1, 2):  # the first twice: one table, counted once
        T._table_at(ws, None, version, index)
    s = ws.clock.summary()
    assert s["multi_page_tables"] == 1 and s["tables_served"] == 2


def test_the_clock_without_any_measurement_summarises_empty_fields():
    s = StageClock().summary()
    assert s["not_checked"] == {} and s["reads"] == {} and s["multi_page_tables"] == 0 and s["tables_served"] == 0
