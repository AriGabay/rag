"""Round 22 U7 (R18, R11, KTD13): the state of each read target, and absence wording only from a completed read.

The workspace carries, per read target, one of: completed (to its end, no unread region), partial (read in part: it names the unread
region), failed (the read broke for a technical reason) and budget-skipped (the tool budget or the reading time stopped the call before
it ran). Only a completed read of the region the datum belongs to may produce absence wording («לא מופיע בסעיף שנבדק»); a read that was
not completed says what it was: not read, read in part, failed. A technical failure is never shown as «not found». The states are the
U0 per-target records (``StageClock.reads``) and the read progress of the sections and tables (``Workspace.reads``), read together by
``Workspace.read_state``. Synthetic text only; no database."""

from __future__ import annotations

import json
import uuid

from app.chat import tools as T
from app.chat.engine import Requested
from app.chat.tools import READ_TO_END, Workspace
from app.chat.verify import TurnRequirements
from tests.unit.test_coverage import _missing_with, _opened, _stopped_on, _ws


def _handlers(monkeypatch, **outputs):
    def make(out):
        def handler(ws, args):
            if isinstance(out, Exception):
                raise out
            return out
        return handler

    monkeypatch.setattr(T, "HANDLERS", {k: make(v) for k, v in outputs.items()})


def _call(ws: Workspace, **target) -> str:
    return T.run_tool(ws, "read", json.dumps({"target": target}))


def _key(**target) -> str:
    return T._read_target("read", {"target": target})


# --- the state of a target ---------------------------------------------------------------------------------------------------

def test_a_section_read_to_its_end_is_completed():
    ws = Workspace(ctx=None)
    target = ("section", "doc", "תיאור הנכס")
    ws.reads[target] = {"to": READ_TO_END, "unread": False, "document_id": "doc"}
    state = ws.read_state(target)
    assert (state.state, state.unread) == (T.READ_COMPLETED, "")


def test_a_section_read_in_part_is_partial_and_names_the_unread_region():
    ws = Workspace(ctx=None)
    clipped, unread = ("section", "doc", "א"), ("section", "doc", "ב")
    ws.reads[clipped] = {"to": 3, "unread": False, "document_id": "doc"}
    ws.reads[unread] = {"to": READ_TO_END, "unread": True, "document_id": "doc"}
    assert (ws.read_state(clipped).state, ws.read_state(clipped).unread) == (T.READ_PARTIAL, "clipped")
    assert (ws.read_state(unread).state, ws.read_state(unread).unread) == (T.READ_PARTIAL, "unread")


def test_a_call_that_broke_is_failed_and_one_the_budget_stopped_is_budget_skipped(monkeypatch):
    ws = Workspace(ctx=None)
    _handlers(monkeypatch, read=RuntimeError("boom"))
    _call(ws, section="§1")
    failed = ws.read_state(_key(section="§1"))
    assert failed.state == T.READ_FAILED
    ws.tool_budget = 10
    _handlers(monkeypatch, read="טקסט ארוך מהתקציב של הסבב")
    _call(ws, section="§2")  # reads, and spends the budget
    assert ws.read_state(_key(section="§2")).state == T.READ_COMPLETED
    out = _call(ws, section="§3")
    assert out.startswith("<not_read")
    skipped = ws.read_state(_key(section="§3"))
    assert skipped.state == T.READ_BUDGET_SKIPPED


def test_a_failed_read_that_is_read_again_is_completed():
    # the same target: the U0 record keeps ``completed`` and clears the failure
    ws = Workspace(ctx=None)
    ws.clock.note_read("read:section=§1", "read", attempted=True, failed_with="tool_failed")
    assert ws.read_state("read:section=§1").state == T.READ_FAILED
    ws.clock.note_read("read:section=§1", "read", attempted=True, completed=True)
    assert ws.read_state("read:section=§1").state == T.READ_COMPLETED


def test_a_target_never_read_has_no_state():
    assert Workspace(ctx=None).read_state(("section", "doc", "x")) is None
    assert Workspace(ctx=None).read_state("read:section=§9") is None


def test_a_refusal_is_not_a_technical_failure(monkeypatch):
    # a read the server refused (a bad handle, a table that does not exist) is the request's fault: not a state that gates a gap line
    ws = Workspace(ctx=None)
    _handlers(monkeypatch, read=T.ToolError("הטבלה לא נמצאה"))
    _call(ws, table="T9")
    assert ws.read_state(_key(table="T9")).state == T.READ_REFUSED


def test_a_visual_reading_that_failed_is_a_technical_failure_not_a_refusal(monkeypatch):
    ws = Workspace(ctx=None)
    _handlers(monkeypatch, inspect=T.ToolError(T.MSG_INSPECT_FAILED.format(what="אזור", status="timeout"), key="read_failed"))
    T.run_tool(ws, "inspect", json.dumps({"target": {"region": "R1"}}))
    assert ws.read_state(T._read_target("inspect", {"target": {"region": "R1"}})).state == T.READ_FAILED


def test_the_documents_of_a_failed_read_are_found_through_the_handles_the_call_names(monkeypatch):
    ws = Workspace(ctx=None)
    doc, other = str(uuid.uuid4()), str(uuid.uuid4())
    ws.handles["D1"] = {"kind": "D", "document_id": doc}
    ws.handles["D2"] = {"kind": "D", "document_id": other}
    ws.handles["§1"] = {"kind": "§", "document_id": doc}
    _handlers(monkeypatch, read=RuntimeError("boom"))
    _call(ws, document="D1", section="§1")
    assert ws.failed_reads([doc]) == [_key(document="D1", section="§1")]
    assert ws.failed_reads([other]) == []
    assert ws.failed_reads(None) == [_key(document="D1", section="§1")]


def test_the_documents_of_a_failed_read_of_a_source_id_are_found_through_the_source(monkeypatch):
    # round 22 review #5: ``read(source=S1)`` names a source (not a handle); its document is the source's, a listing has none
    ws = _ws()
    doc = str(ws.sources["S1"].document_id)
    _handlers(monkeypatch, read=RuntimeError("boom"))
    _call(ws, source="S1")
    assert ws.failed_reads([doc]) == [_key(source="S1")]
    assert ws.failed_reads([str(uuid.uuid4())]) == []


def test_a_failed_read_whose_document_cannot_be_resolved_fails_every_document(monkeypatch):
    ws = _ws()
    doc = str(ws.sources["S1"].document_id)
    _handlers(monkeypatch, read=RuntimeError("boom"))
    _call(ws, source="S99")  # names nothing the workspace knows
    assert ws.failed_reads([doc]) == [_key(source="S99")]
    assert ws.failed_reads([str(uuid.uuid4())]) == [_key(source="S99")]


# --- absence wording follows the state --------------------------------------------------------------------------------------

def _fail_read_of(ws: Workspace, monkeypatch, **target) -> str:
    doc = str(ws.sources["S1"].document_id)
    ws.handles["D1"] = {"kind": "D", "document_id": doc}
    _handlers(monkeypatch, read=RuntimeError("boom"))
    _call(ws, document="D1", **target)
    return doc


def test_a_search_alone_does_not_say_not_found_when_a_read_of_the_turn_failed(monkeypatch):
    ws = _ws()
    ws.searches.append("דמי ניהול")
    _fail_read_of(ws, monkeypatch, section="§1")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ["H1"])
    assert outcomes[1]["limitation"] == "tool_failure"
    assert gap == "**דמי הניהול** לא הושלם בגלל תקלה בכלי או בשירות המודל בזמן הבדיקה."
    assert "לא נמצא" not in gap and "לא מופיע" not in gap


def test_a_reading_the_judge_tied_to_the_component_that_is_not_opened_says_failed_too_when_its_document_failed(monkeypatch):
    ws = _ws()
    _fail_read_of(ws, monkeypatch, section="§1")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ["S1"])
    assert outcomes[1]["limitation"] == "tool_failure"
    assert "לא נמצא" not in gap and "לא מופיע" not in gap


def _claim(ws: Workspace) -> Requested:
    """The model's claim that the component was not found, tied to S1's document (which the turn touched)."""
    doc = str(ws.sources["S1"].document_id)
    ws.touch(doc, "דוח הדגמה", "retrieved")
    ws.searches.append("דמי ניהול")
    return Requested(component="N2", label="דמי הניהול", document_ids=[doc], status="not_found_search", checked_where="")


def test_a_failed_read_of_the_documents_the_component_is_tied_to_says_failed(monkeypatch):
    ws = _ws()
    claim = _claim(ws)
    _fail_read_of(ws, monkeypatch, section="§1")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), [], requested=[claim])
    assert outcomes[1]["limitation"] == "tool_failure"
    assert "לא נמצא" not in gap and "לא מופיע" not in gap


def test_a_crashed_read_of_a_source_id_blocks_the_absence_wording_for_its_document(monkeypatch):
    ws = _ws()
    claim = _claim(ws)
    _handlers(monkeypatch, read=RuntimeError("boom"))
    _call(ws, source="S1")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), [], requested=[claim])
    assert outcomes[1]["limitation"] == "tool_failure"
    assert "לא נמצא" not in gap and "לא מופיע" not in gap


def test_a_failed_read_of_another_document_does_not_change_the_line_of_a_component_tied_to_its_own(monkeypatch):
    ws = _ws()
    claim = _claim(ws)
    ws.handles["D2"] = {"kind": "D", "document_id": str(uuid.uuid4())}
    _handlers(monkeypatch, read=RuntimeError("boom"))
    _call(ws, document="D2", section="§1")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), [], requested=[claim])
    assert outcomes[1]["limitation"] == "not_located"
    assert "תקלה" not in gap  # the same line as without that failure


def test_a_completed_read_with_no_finding_still_says_not_present_in_that_section():
    ws = _ws()
    _opened(ws)
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ["S1"])
    assert outcomes[1]["limitation"] == "not_in_part_read"
    assert gap == "**דמי הניהול** לא מופיע בסעיף \"תיאור הנכס\" שנבדק [S1]."


def test_a_complete_read_of_the_section_beats_a_failed_read_of_another_part_of_the_document(monkeypatch):
    # the statement is limited to the section that was read to its end: what failed elsewhere does not unsay it
    ws = _ws()
    _opened(ws)
    _fail_read_of(ws, monkeypatch, section="§2")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ["S1"])
    assert outcomes[1]["limitation"] == "not_in_part_read"


def test_a_partial_read_names_the_unread_region_and_never_says_not_present():
    clipped = _ws()
    _opened(clipped, complete=False)
    _, outcomes, gap = _missing_with(clipped, TurnRequirements(), ["S1"])
    assert outcomes[1]["limitation"] == "region_not_read" and outcomes[1]["variant"] == "clipped"
    assert "נקרא רק בחלקו" in gap and "תיאור הנכס" in gap and "לא מופיע" not in gap
    unread = _ws()
    _opened(unread, unread=True)
    _, outcomes, gap = _missing_with(unread, TurnRequirements(), ["S1"])
    assert outcomes[1]["limitation"] == "region_not_read" and outcomes[1]["variant"] == "unread"
    assert "אזורים שלא נקראו" in gap and "לא מופיע" not in gap


def test_a_budget_skipped_component_says_it_was_not_read_never_not_found(monkeypatch):
    ws = _ws()
    ws.searches.append("דמי ניהול")
    ws.tool_budget = 10
    doc = str(ws.sources["S1"].document_id)
    ws.handles["D1"] = {"kind": "D", "document_id": doc}
    _handlers(monkeypatch, read="טקסט ארוך מהתקציב של הסבב")
    _call(ws, document="D1", section="§1")  # spends the budget
    out = _call(ws, document="D1", section="§2")
    assert out.startswith("<not_read")
    _, outcomes, gap = _missing_with(ws, TurnRequirements(), ["H1"])
    assert outcomes[1]["limitation"] == "limit_reached" and outcomes[1]["variant"] == "tool_budget"
    assert "לא אותר במסגרת מגבלת היקף הקריאה" in gap
    assert "לא נמצא" not in gap and "לא מופיע" not in gap


def test_a_stop_by_the_reading_limit_is_not_a_failure():
    ws = _ws()
    ws.searches.append("דמי ניהול")
    _stopped_on(ws, "time_limit")
    _, outcomes, _ = _missing_with(ws, TurnRequirements(), ["H1"])
    assert outcomes[1]["limitation"] == "limit_reached"
