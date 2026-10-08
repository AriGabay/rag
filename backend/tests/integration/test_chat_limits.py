"""Turn limits and a cache-friendly loop (U13, KTD12, R30, AE8).

The model is scripted and keeps reading for as long as it is allowed; a step sent with ``tool_choice`` "none"
returns its scripted final answer, as a model honouring it would. These tests prove the server's behaviour: the
per-turn tool-output budget, the step bound and the time reserve each end the reading with a forced final step
that keeps the same tools (so the cached prefix survives) and tells the model it ran out; the answer is still
verified, is marked partial and names the limit; a turn that has no time left to verify fails with its own message
and its calls are still logged; and every step only appends to the items the previous step sent. Synthetic
documents only."""

from __future__ import annotations

import copy
import json
import re

import pytest
from sqlalchemy import text

from app.chat import api, engine
from app.chat import tools as T
from app.db import tenant_tx
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_reading_tools import add_document
from tests.support.scripted_agent import ScriptedAgent, call, final, read

pytestmark = pytest.mark.db

PLOT = "שטח המגרש הוא 812 מ\"ר [S2]."
ANSWER = final(PLOT, claims=[{"text": "שטח המגרש הוא 812 מ\"ר", "source_ids": ["S2"], "basis": "explicit"}])


@pytest.fixture
def office(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    a = make_office(db, "משרד א", "admin-a@example.test")
    a.doc, a.ver = add_document(a)
    return a


def setting(monkeypatch, **values) -> None:
    from app.config import get_settings

    for k, v in values.items():
        monkeypatch.setattr(get_settings(), k, v)


def pages(document: str, first: int, last: int | None = None) -> dict:
    return read(pages={"document": document, "from_page": first, "to_page": last or first})


class Reader(ScriptedAgent):
    """A model that reads the next scripted step for as long as it may call tools, and answers when the step is
    sent with ``tool_choice`` "none" (or its reads run out). Every step records a deep copy of its items, its
    instructions, its tools and its tool choice."""

    def __init__(self, reads: list, answer: dict, **kw) -> None:
        super().__init__([], **kw)
        self.reads, self.answer = list(reads), answer
        self.snapshots: list[list] = []
        self.instructions: list[str] = []
        self.tools: list[str] = []
        self.choices: list[str | None] = []

    def agent_step(self, instructions, items, tools, final_schema, **kw):
        self.snapshots.append(copy.deepcopy(items))
        self.instructions.append(instructions)
        self.tools.append(json.dumps(tools, ensure_ascii=False))
        self.choices.append(kw.get("tool_choice"))
        forced = kw.get("tool_choice") == "none" or not self.reads
        self.steps = [self.answer if forced else self.reads.pop(0)]
        return super().agent_step(instructions, items, tools, final_schema, **kw)


def ask(client, office, monkeypatch, agent, question: str = "מה שטח המגרש?", cid: str | None = None) -> dict:
    cloud(monkeypatch, office, agent)
    login(client, "admin-a@example.test")
    return send(client, cid or new_conversation(client), question)


def diagnostics(client, m: dict) -> dict:
    r = client.get(f"/api/chat/messages/{m['id']}/diagnostics")
    assert r.status_code == 200, r.text
    return r.json()


TOOLS = json.dumps(T.TOOLS, ensure_ascii=False)


def assert_forced_final(agent: Reader, limit_words: str) -> None:
    """The last step kept every tool in its order, could not call one, and was told why."""
    assert agent.tools == [TOOLS] * len(agent.tools)  # never an empty list: the cached prefix stays the same
    assert agent.choices[:-1] == [None] * (len(agent.choices) - 1) and agent.choices[-1] == "none"
    notice = agent.snapshots[-1][-1]
    assert notice["role"] == "user" and limit_words in notice["content"] and "partial" in notice["content"]


# --- the tool-output budget (AE8) ---------------------------------------------------------------------------------

def test_a_model_that_keeps_reading_exhausts_the_budget_and_gets_a_verified_partial_answer(client, office, monkeypatch):
    setting(monkeypatch, chat_tool_output_chars=1500)
    agent = Reader([[pages(office.doc, 1)], [pages(office.doc, 2)], [pages(office.doc, 3)], [pages(office.doc, 4)]],
                   ANSWER)
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    a = m["answer"]
    # page 2 crossed the budget: nothing more was read, and the next step had to answer
    assert len(agent.snapshots) == 3 and "הבניין בן חמש קומות" not in str(agent.snapshots[-1])
    assert_forced_final(agent, "מגבלת היקף הקריאה")
    # verified, partial, and the limit is named in plain words; the claim it supports stays
    assert a["verification"]["judged"] and a["verification"]["removed"] == 0
    assert a["status"] == "partial" and "812" in a["markdown"] and "[S2]" in a["markdown"]
    assert "במגבלת הקריאה לשאלה אחת" in a["markdown"]
    assert a["limits_hit"] == ["tool_budget"] and diagnostics(client, m)["limits_hit"] == ["tool_budget"]


def test_after_the_budget_reading_tools_return_only_a_header_and_how_to_read_on(office):
    ws = T.Workspace(ctx=office.ctx(), tool_budget=1500)
    ws.user_messages = [{"turn": 1, "text": "ומה אם השווי יעלה ב-5%?", "current": True}]
    target = {"target": pages(office.doc, 2)["arguments"]["target"]}
    first = T.run_tool(ws, "read", json.dumps(target, ensure_ascii=False))
    assert "שטח המגרש 812" in first and ws.limits_hit == ["tool_budget"] and ws.tool_chars == len(first)
    before = (dict(ws.sources), dict(ws.reads), {k: dict(v) for k, v in ws.activity.items()})
    later = T.run_tool(ws, "read", json.dumps({"target": pages(office.doc, 3)["arguments"]["target"]}))
    # a header with how to read on, and no body: nothing of page 3 was sent, read or registered
    assert later.startswith('<not_read tool="read" status="tool_budget"') and 'more="read(pages=' in later
    assert "הבניין בן חמש קומות" not in later and "פסקה 4" not in later
    assert (ws.sources, ws.reads, ws.activity) == before
    search = T.run_tool(ws, "search", json.dumps({"query": "שטח המגרש", "document_ids": None, "limit": None}))
    assert search.startswith('<not_read tool="search"') and "812" not in search
    # what does not read the documents still works: a user's scenario number is registered
    assert T.run_tool(ws, "assume", json.dumps({"value": "5%", "quote": "יעלה ב-5%", "label": "עלייה"})).startswith("A1")
    assert ws.limits_hit == ["tool_budget"]


def test_parallel_reads_of_one_step_stop_at_the_budget(client, office, monkeypatch):
    setting(monkeypatch, chat_tool_output_chars=1500)
    agent = Reader([[pages(office.doc, 2), pages(office.doc, 3)]], ANSWER)
    m = ask(client, office, monkeypatch, agent)
    outputs = agent.tool_outputs(1)
    assert "שטח המגרש 812" in outputs[0] and outputs[1].startswith('<not_read tool="read"')
    assert m["answer"]["status"] == "partial" and m["answer"]["limits_hit"] == ["tool_budget"]


def test_a_turn_within_the_budget_is_not_marked(client, office, monkeypatch):
    # one read, then the model answers on its own
    agent = Reader([[pages(office.doc, 2)]], final("שטח המגרש הוא 812 מ\"ר [S1]."))
    m = ask(client, office, monkeypatch, agent)
    a = m["answer"]
    assert a["status"] == "answered" and a["limits_hit"] == [] and "מגבלת" not in a["markdown"]
    assert agent.choices == [None, None] and agent.tools == [TOOLS, TOOLS]


# --- the step bound and the time reserve ---------------------------------------------------------------------------

def test_the_step_limit_ends_reading_with_a_verified_partial_answer(client, office, monkeypatch):
    # eight steps, two kept for the repair rounds: the reading may take six, the sixth of them forced to answer
    setting(monkeypatch, chat_max_steps=8, chat_repair_rounds=2)
    agent = Reader([[pages(office.doc, p)] for p in (1, 2, 3, 4, 1, 2, 3, 4)], ANSWER)
    m = ask(client, office, monkeypatch, agent)
    a = m["answer"]
    assert len(agent.snapshots) == 6
    assert_forced_final(agent, "מספר הצעדים המרבי")
    assert a["verification"]["judged"] and a["verification"]["removed"] == 0
    assert a["status"] == "partial" and "812" in a["markdown"] and "מספר הצעדים המרבי לשאלה אחת" in a["markdown"]
    assert a["limits_hit"] == ["step_limit"] and diagnostics(client, m)["limits_hit"] == ["step_limit"]


def test_the_repair_rounds_fit_inside_the_step_bound(client, office, monkeypatch):
    setting(monkeypatch, chat_max_steps=4, chat_repair_rounds=2)
    wrong = final("שטח המגרש הוא 990 מ\"ר [S1].")
    agent = Reader([[pages(office.doc, 2)], [pages(office.doc, 3)], [pages(office.doc, 4)]], wrong)
    m = ask(client, office, monkeypatch, agent)
    # two reading steps (the second forced), one repair step and one rewrite: never more than the bound
    assert len(agent.snapshots) == 4 and agent.choices == [None, "none", "none", "none"]
    assert agent.tools == [TOOLS] * 4
    assert m["status"] == "done" and "990" not in m["answer"]["markdown"]
    assert len(diagnostics(client, m)["rounds"]) == 3


def test_the_time_reserve_stops_reading_before_verification_has_no_time(client, office, monkeypatch):
    # the reserve covers the whole turn: the first step must already answer
    setting(monkeypatch, chat_turn_seconds=150, chat_verify_reserve_seconds=150)
    agent = Reader([[pages(office.doc, 2)]], final("לא נמצא נתון על שטח המגרש בקריאה שבוצעה.", "not_found"))
    m = ask(client, office, monkeypatch, agent)
    a = m["answer"]
    assert len(agent.snapshots) == 1
    assert_forced_final(agent, "הזמן לחיפוש ולקריאה")
    assert a["status"] == "partial" and "מגבלת הזמן לשאלה אחת" in a["markdown"]
    assert a["limits_hit"] == ["time_limit"]


def test_a_turn_that_cannot_be_verified_in_time_fails_with_its_message_and_logs_its_calls(client, office, monkeypatch):
    setting(monkeypatch, chat_verify_min_seconds=10_000)  # less time is left than a verification needs
    with tenant_tx(office.system) as conn:
        conn.execute(text("DELETE FROM provider_usage"))
    agent = Reader([[pages(office.doc, 2)]], ANSWER)
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "failed" and m["answer"] is None and m["content"] == ""
    assert api.FAILURE_TEXT["verify_no_time"] in m["error"] and "לאמת" in m["error"]
    assert not any(c.purpose.value == "verify" for c in agent.calls)  # the judge was never called
    assert [u["purpose"] for u in m["usage"]] == ["agent", "agent"]
    with tenant_tx(office.system) as conn:
        assert [r.purpose for r in conn.execute(text("SELECT purpose FROM provider_usage ORDER BY id"))] == [
            "agent", "agent"]


# --- a cache-friendly loop ---------------------------------------------------------------------------------------

def test_every_step_sends_the_previous_steps_items_unchanged_and_only_appends(client, office, monkeypatch):
    setting(monkeypatch, chat_tool_output_chars=4000)
    agent = Reader([[pages(office.doc, 1)], [pages(office.doc, 2)], [pages(office.doc, 3)]], ANSWER)
    ask(client, office, monkeypatch, agent)
    assert len(agent.snapshots) >= 3
    for before, after in zip(agent.snapshots, agent.snapshots[1:], strict=False):
        assert after[:len(before)] == before and len(after) > len(before)
    assert len(set(agent.instructions)) == 1 and agent.instructions[0] == engine.POLICY


def test_the_tools_and_instructions_are_identical_across_turns_and_on_the_forced_step(client, office, monkeypatch):
    first = Reader([[call("search", query="שטח המגרש", document_ids=None, limit=None)]],
                   final("שטח המגרש הוא 812 מ\"ר [S1]."))
    cloud(monkeypatch, office, first)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    assert send(client, cid, "מה שטח המגרש?")["status"] == "done"
    setting(monkeypatch, chat_tool_output_chars=1500)
    second = Reader([[pages(office.doc, 2)], [pages(office.doc, 3)]], ANSWER)
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: second)
    send(client, cid, "ומה עוד כתוב בתיאור הנכס?")
    assert second.choices[-1] == "none"
    assert first.tools + second.tools == [TOOLS] * (len(first.tools) + len(second.tools))
    assert set(first.instructions + second.instructions) == {engine.POLICY}
    # nothing of the moment the turn ran is in what every step repeats
    assert not re.search(r"\d{4}-\d{2}-\d{2}|\d{1,2}:\d{2}", engine.POLICY + TOOLS)

