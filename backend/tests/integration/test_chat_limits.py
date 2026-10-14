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
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.chat import api, engine
from app.chat import tools as T
from app.db import tenant_tx
from app.providers.llm import CallStatus, Purpose
from tests.conftest import login
from tests.factories import make_office, make_user
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_reading_tools import add_document
from tests.integration.test_search import add_chunks
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

    def __init__(self, reads: list, answer: dict, compute: list | None = None, **kw) -> None:
        super().__init__([], **kw)
        self.reads, self.answer = list(reads), answer
        self.compute = list(compute or [])  # the steps taken while only the compute tools are allowed
        self.snapshots: list[list] = []
        self.instructions: list[str] = []
        self.tools: list[str] = []
        self.choices: list[str | None] = []

    def agent_step(self, instructions, items, tools, final_schema, **kw):
        self.snapshots.append(copy.deepcopy(items))
        self.instructions.append(instructions)
        self.tools.append(json.dumps(tools, ensure_ascii=False))
        choice = kw.get("tool_choice")
        self.choices.append(choice)
        forced = choice == "none" or not self.reads
        if isinstance(choice, dict):  # only some tools are allowed: the next compute step, else the answer
            self.steps = [self.compute.pop(0) if self.compute else self.answer]
        else:
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


COMPUTE = {"calculate", "take_value", "assume"}


def compute_only(choice) -> bool:
    """A tool choice that allows the compute tools and nothing else (the step after the tool budget was spent)."""
    return (isinstance(choice, dict) and choice["type"] == "allowed_tools" and choice["mode"] == "auto"
            and {t["name"] for t in choice["tools"]} == COMPUTE)


def assert_forced_final(agent: Reader, limit_words: str, compute: bool = False) -> None:
    """The last step kept every tool in its order, could not call one (``compute``: but a compute tool), and was told why."""
    assert agent.tools == [TOOLS] * len(agent.tools)  # never an empty list: the cached prefix stays the same
    assert agent.choices[:-1] == [None] * (len(agent.choices) - 1)
    assert compute_only(agent.choices[-1]) if compute else agent.choices[-1] == "none"
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
    assert_forced_final(agent, "מגבלת היקף הקריאה", compute=True)
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


SCENARIO_QUESTION = "ומה אם השווי יעלה ב-5%?"
SCENARIO = [call("assume", value="5%", quote="יעלה ב-5%", label="עלייה בשווי")]


def test_with_the_budget_spent_the_last_step_may_still_register_and_compute_on_what_was_read(client, office, monkeypatch):
    """The diagnosed lock-out: the notice said computing was still possible, but the step was sent with every tool
    forbidden, so a scenario was never computed although its inputs were read. Reading stays refused."""
    setting(monkeypatch, chat_tool_output_chars=1500)
    agent = Reader([[pages(office.doc, 1)], [pages(office.doc, 2)]], ANSWER, compute=[SCENARIO])
    m = ask(client, office, monkeypatch, agent, SCENARIO_QUESTION)
    assert m["status"] == "done", m
    # the budget was spent by page 2; the next step could compute (and did), and the one after it answered
    assert len(agent.snapshots) == 4 and agent.choices[:2] == [None, None]
    assert compute_only(agent.choices[2]) and compute_only(agent.choices[3])
    assert agent.tools == [TOOLS] * 4  # the same tools in the same order: the cached prefix survives
    assert agent.tool_outputs(3)[-1].startswith("A1")  # the assumption was registered
    notice = agent.snapshots[2][-1]
    assert notice["role"] == "user" and "מגבלת היקף הקריאה" in notice["content"] and "partial" in notice["content"]
    assert "calculate" in notice["content"] and "אי אפשר לקרוא" in notice["content"]
    assert m["answer"]["status"] == "partial" and m["answer"]["limits_hit"] == ["tool_budget"]
    assert m["answer"]["verification"]["judged"] and "812" in m["answer"]["markdown"]


def test_after_the_budget_a_reading_tool_called_in_a_compute_step_is_still_refused(client, office, monkeypatch):
    setting(monkeypatch, chat_tool_output_chars=1500)
    agent = Reader([[pages(office.doc, 1)], [pages(office.doc, 2)]], ANSWER, compute=[[pages(office.doc, 3)]])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert agent.tool_outputs(3)[-1].startswith('<not_read tool="read" status="tool_budget"')
    assert "הבניין בן חמש קומות" not in str(agent.snapshots[-1])


def test_compute_steps_after_the_budget_are_bounded_and_the_step_after_them_must_answer(client, office, monkeypatch):
    setting(monkeypatch, chat_tool_output_chars=1500)
    agent = Reader([[pages(office.doc, 1)], [pages(office.doc, 2)]], ANSWER, compute=[SCENARIO] * 10)
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    allowed = [compute_only(c) for c in agent.choices]
    assert allowed == [False, False] + [True] * engine.COMPUTE_STEPS_AFTER_BUDGET + [False]
    assert agent.choices[-1] == "none" and m["answer"]["limits_hit"] == ["tool_budget"]
    # the final step is told once more that it answers now
    assert "אין עוד קריאה לכלים" in agent.snapshots[-1][-1]["content"]


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

def notices(snapshot: list) -> list[int]:
    """The positions of the near-limit notice among a step's items."""
    return [i for i, item in enumerate(snapshot) if item.get("role") == "user" and item["content"] == engine.NEAR_LIMIT_NOTICE]


def test_the_step_limit_ends_reading_with_a_verified_partial_answer(client, office, monkeypatch):
    # the defaults: fourteen steps, two kept for the repair rounds — the reading may take twelve, the twelfth of them
    # forced to answer
    agent = Reader([[pages(office.doc, p)] for p in (1, 2, 3, 4) * 5], ANSWER)
    m = ask(client, office, monkeypatch, agent)
    a = m["answer"]
    assert len(agent.snapshots) == 12
    assert_forced_final(agent, "מספר הצעדים המרבי")
    assert a["verification"]["judged"] and a["verification"]["removed"] == 0
    assert a["status"] == "partial" and "812" in a["markdown"] and "מספר הצעדים המרבי לשאלה אחת" in a["markdown"]
    assert a["limits_hit"] == ["step_limit"] and diagnostics(client, m)["limits_hit"] == ["step_limit"]


def test_two_tool_steps_before_the_step_limit_the_model_is_told_once_to_compute_and_answer(client, office, monkeypatch):
    agent = Reader([[pages(office.doc, p)] for p in (1, 2, 3, 4) * 5], ANSWER)
    ask(client, office, monkeypatch, agent)
    # appended as the last item of the tenth step (two tool steps left, then the forced twelfth), and only then
    assert [notices(s) for s in agent.snapshots[:9]] == [[]] * 9
    assert notices(agent.snapshots[9]) == [len(agent.snapshots[9]) - 1]
    assert all(len(notices(s)) == 1 for s in agent.snapshots[9:])
    assert "calculate" in engine.NEAR_LIMIT_NOTICE and "באותו צעד" in engine.NEAR_LIMIT_NOTICE
    # the steps after it still only append, and the forced final step behaves as before
    for before, after in zip(agent.snapshots, agent.snapshots[1:], strict=False):
        assert after[:len(before)] == before
    assert agent.choices[9:11] == [None, None]
    assert_forced_final(agent, "מספר הצעדים המרבי")


def test_the_step_bound_is_configurable_and_the_notice_follows_it(client, office, monkeypatch):
    # eight steps, two kept for the repair rounds: the reading may take six, the notice comes at the fourth
    setting(monkeypatch, chat_max_steps=8, chat_repair_rounds=2)
    agent = Reader([[pages(office.doc, p)] for p in (1, 2, 3, 4, 1, 2, 3, 4)], ANSWER)
    m = ask(client, office, monkeypatch, agent)
    assert len(agent.snapshots) == 6 and m["answer"]["limits_hit"] == ["step_limit"]
    assert [len(notices(s)) for s in agent.snapshots] == [0, 0, 0, 1, 1, 1]
    assert_forced_final(agent, "מספר הצעדים המרבי")


def test_a_model_that_answers_before_the_notice_never_gets_it(client, office, monkeypatch):
    agent = Reader([[pages(office.doc, 2)], [pages(office.doc, 3)]], final("שטח המגרש הוא 812 מ\"ר [S1]."))
    m = ask(client, office, monkeypatch, agent)
    assert m["answer"]["limits_hit"] == [] and not any(notices(s) for s in agent.snapshots)


def test_the_repair_rounds_fit_inside_the_step_bound(client, office, monkeypatch):
    setting(monkeypatch, chat_max_steps=4, chat_repair_rounds=2)
    wrong = final("שטח המגרש הוא 990 מ\"ר [S1].")
    agent = Reader([[pages(office.doc, 2)], [pages(office.doc, 3)], [pages(office.doc, 4)]], wrong)
    m = ask(client, office, monkeypatch, agent)
    # two reading steps (the second forced), one repair step and one rewrite: never more than the bound
    assert len(agent.snapshots) == 4 and agent.choices == [None, "none", "none", "none"]
    assert agent.tools == [TOOLS] * 4
    assert not any(notices(s) for s in agent.snapshots)  # a bound too short for the notice gets only the limit
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
    assert [u["purpose"] for u in m["usage"]] == ["agent", "request", "agent"]  # the request analysis (round 7 KTD1) is a call of its own
    with tenant_tx(office.system) as conn:
        assert [r.purpose for r in conn.execute(text("SELECT purpose FROM provider_usage ORDER BY id"))] == [
            "agent", "request", "agent"]


# --- a deadline inside the tools (round 9 U11, R16, R18, KTD9) --------------------------------------------------------

READING_BY_DEADLINE = ["search", "read", "outline", "find_documents", "list_documents", "find_measurements"]


@pytest.mark.parametrize("name", READING_BY_DEADLINE)
def test_a_reading_tool_started_after_the_reading_deadline_is_refused_without_running(monkeypatch, name):
    import time

    ran = []
    monkeypatch.setattr(T, "HANDLERS", {n: (lambda ws, a, n=n: ran.append(n) or "תוצאה") for n in
                                        [*READING_BY_DEADLINE, "inspect", "calculate", "take_value", "assume"]})
    ws = T.Workspace(ctx=None, read_until=time.monotonic() + 100)
    assert T.run_tool(ws, name, "{}") == "תוצאה" and ran == [name] and ws.limits_hit == []  # before the deadline
    ran.clear()
    ws.read_until = time.monotonic() - 1
    out = T.run_tool(ws, name, "{}")
    assert out.startswith(f'<not_read tool="{name}" status="time_limit"') and "הזמן" in out
    assert ran == [] and ws.limits_hit == ["time_limit"]
    T.run_tool(ws, name, "{}")
    assert ws.limits_hit == ["time_limit"]  # recorded once
    # what is not reading goes on (a vision call keeps its own check, inside the tool)
    for other in ("inspect", "calculate", "take_value", "assume"):
        assert T.run_tool(ws, other, "{}") == "תוצאה"
    assert ran == ["inspect", "calculate", "take_value", "assume"]


def test_a_slow_read_makes_the_next_one_of_the_step_refuse_with_a_limit_notice_and_the_turn_names_the_time_limit(
        client, office, monkeypatch):
    from tests.integration.test_chat_inspect import Clock

    setting(monkeypatch, chat_turn_seconds=150, chat_verify_reserve_seconds=40, chat_verify_min_seconds=15)
    clock = Clock(monkeypatch)
    real = T.HANDLERS["read"]

    def slow(ws, args):
        clock.offset += 120  # one slow read takes the turn past its reading deadline (110 s)
        return real(ws, args)

    monkeypatch.setitem(T.HANDLERS, "read", slow)
    agent = Reader([[pages(office.doc, 2), pages(office.doc, 3)]],
                   final(PLOT.replace("S2", "S1"), claims=[{"text": "שטח המגרש הוא 812 מ\"ר", "source_ids": ["S1"],
                                                            "basis": "explicit"}]))
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    first, second = agent.tool_outputs(1)
    assert "שטח המגרש 812" in first
    assert second.startswith('<not_read tool="read" status="time_limit"') and "הבניין בן חמש קומות" not in second
    assert agent.choices == [None, "none"]
    a = m["answer"]
    assert a["status"] == "partial" and a["limits_hit"] == ["time_limit"] and "מגבלת הזמן לשאלה אחת" in a["markdown"]
    # the user sees that the second read was stopped by the time limit, not that nothing was found
    done = [p["label"] for p in m["progress"] if p["step"] == "read" and p.get("done")]
    assert done == ["נקראו עמודים 2–2", "נעצר: מגבלת הזמן"]


# --- the stage clock and the progress events (round 9 U1, R16, R17, KTD9) ------------------------------------------

VALUE = "סיכום: השווי למ\"ר בנוי במבנה הגפן הוא 9,500 ₪."
VALUE_ANSWER = final("השווי למ\"ר בנוי הוא 9,500 ₪ [S1].")


def timed_turn(client, office, monkeypatch) -> dict:
    """Two searches in one step, then a read of pages, then a verified answer from the first search."""
    add_chunks(office, office.default_group_id, [VALUE], "9" * 64)
    searches = [call("search", query="שווי למ\"ר", document_ids=None, limit=None),
                call("search", query="מבנה הגפן", document_ids=None, limit=None)]
    m = ask(client, office, monkeypatch, Reader([searches, [pages(office.doc, 2)]], VALUE_ANSWER))
    assert m["status"] == "done", m
    return m


def test_a_turn_records_its_tool_timings_and_stage_totals_in_the_diagnostics(client, office, monkeypatch):
    m = timed_turn(client, office, monkeypatch)
    timing = diagnostics(client, m)["timing"]
    assert [(t["tool"], t["stage"]) for t in timing["tools"]] == [("search", "locate"), ("search", "locate"),
                                                                  ("read", "read")]
    assert all(t["seconds"] >= 0 and t["chars"] > 0 for t in timing["tools"])
    stages = timing["stages"]  # a jsonb object: the keys come back in its own order
    assert {k for k, v in stages.items() if v["calls"]} == {"locate", "read", "compose", "verify", "final"}
    assert set(stages) == {"locate", "read", "compute", "compose", "verify", "final"}
    assert stages["locate"]["calls"] == 2 and stages["read"]["calls"] == 1 and stages["compute"]["calls"] == 0
    # the stages are disjoint parts of the turn; what no stage holds is reported, never hidden
    held = sum(v["seconds"] for v in stages.values())
    assert held <= timing["total_seconds"] and timing["unattributed_seconds"] >= 0
    assert abs(timing["total_seconds"] - held - timing["unattributed_seconds"]) < 0.01
    # no content: tool names, stages and numbers only
    assert "9,500" not in json.dumps(timing, ensure_ascii=False)
    # the same row, the same gate: another employee of the office reads nothing, and the answer carries none of it
    make_user(office, "emp2@example.test", [])
    login(client, "emp2@example.test")
    assert client.get(f"/api/chat/messages/{m['id']}/diagnostics").status_code == 404
    assert "timing" not in json.dumps(m["answer"])


def test_a_first_turn_keeps_the_resolution_to_its_intent_beside_its_timing(client, office, monkeypatch):
    m = ask(client, office, monkeypatch, Reader([], ANSWER))
    d = diagnostics(client, m)
    # no resolve call on a first turn (no parse, no lookup): only the intent the request analysis settled (round 10)
    # and the turn's time mode (round 11 U7)
    assert set(d["resolution"]) == {"intent", "decisions", "time"} and d["resolution"]["intent"]["value"] == "unknown"
    assert d["timing"]["stages"]["compose"]["calls"] >= 1


def test_every_tool_gets_a_timestamped_start_and_a_done_event_that_says_what_it_found(client, office, monkeypatch):
    m = timed_turn(client, office, monkeypatch)
    events = [p for p in m["progress"] if p["step"] != "queued"]
    stamps = [p["at"] for p in events]
    assert stamps == sorted(stamps) and all(isinstance(t, float) for t in stamps)
    tools = [(p["step"], bool(p.get("done"))) for p in events if p["step"] in ("search", "read")]
    assert tools == [("search", False), ("search", True), ("search", False), ("search", True), ("read", False),
                     ("read", True)]
    done = [p["label"] for p in events if p.get("done")]
    assert done[0].startswith("נמצאו ") and "קטעים" in done[0] and done[2] == "נקראו עמודים 2–2"
    # the start labels stay as they were
    assert [p["label"] for p in events if p["step"] == "search" and not p.get("done")] == [
        'מחפש: שווי למ"ר', "מחפש: מבנה הגפן"]
    # the verification step has no done event: its end is the answer
    assert events[-1]["step"] == "verify" and "done" not in events[-1]


# --- truncated model output ------------------------------------------------------------------------------------

class TruncatedAgent(ScriptedAgent):
    def __init__(self, steps, **kw):
        super().__init__(steps, **kw)
        self.options = []

    def agent_step(self, instructions, items, tools, final_schema, **kw):
        self.options.append(dict(kw))
        step = super().agent_step(instructions, items, tools, final_schema, **kw)
        if step.status == CallStatus.INCOMPLETE:
            step.detail = "max_output_tokens"
            step.output_tokens = kw.get("max_output_tokens", 6000)
            # An unfinished response is never used as input or executed, even if it contains a tool call.
            step.output = [{"role": "assistant", "content": "פלט קטוע שאסור להציג"}]
            step.calls = [SimpleNamespace(name="must_not_run")]
        return step


def test_a_truncated_answer_retries_with_more_tokens_and_is_verified(client, office, monkeypatch):
    agent = TruncatedAgent([[pages(office.doc, 2)], CallStatus.INCOMPLETE,
                            final('שטח המגרש הוא 812 מ"ר [S1].')])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert m["answer"]["verification"]["judged"] and "812" in m["answer"]["markdown"]
    assert [o["max_output_tokens"] for o in agent.options] == [12000, 12000, 24000]
    assert agent.seen[1] == agent.seen[2]  # same evidence, without any interrupted output
    assert agent.options[1]["cache_key"] == agent.options[2]["cache_key"]
    assert "retry" in [p["step"] for p in m["progress"]]
    assert [u["status"] for u in m["usage"] if u["purpose"] == "agent"] == ["ok", "incomplete", "ok"]
    assert any(c.purpose == Purpose.VERIFY for c in agent.calls)


def test_a_second_truncation_fails_with_actionable_text_and_no_partial_answer(client, office, monkeypatch):
    agent = TruncatedAgent([CallStatus.INCOMPLETE, CallStatus.INCOMPLETE, final("אסור להציג")])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "failed" and m["answer"] is None and not m["content"]
    assert "אורך" in m["error"] and "סעיפים" in m["error"]
    assert len(agent.seen) == 2
    assert [u["status"] for u in m["usage"] if u["purpose"] == "agent"] == ["incomplete", "incomplete"]
    assert not any(c.purpose == Purpose.VERIFY for c in agent.calls)


def test_the_output_retry_is_once_per_turn_even_when_the_retry_calls_a_tool(client, office, monkeypatch):
    agent = TruncatedAgent([CallStatus.INCOMPLETE, [pages(office.doc, 2)], CallStatus.INCOMPLETE,
                            final('שטח המגרש הוא 812 מ"ר [S1].')])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "failed" and len(agent.seen) == 3
    assert [o["max_output_tokens"] for o in agent.options] == [12000, 24000, 24000]


@pytest.mark.parametrize("failure", [CallStatus.INCOMPLETE, CallStatus.REFUSAL, CallStatus.TIMEOUT])
def test_a_failure_without_the_output_limit_reason_is_not_retried(client, office, monkeypatch, failure):
    agent = ScriptedAgent([failure, final("אסור להציג")])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "failed" and len(agent.seen) == 1
    assert "retry" not in [p["step"] for p in m["progress"]]


def test_an_output_retry_keeps_the_forced_final_choice_and_the_step_bound(client, office, monkeypatch):
    setting(monkeypatch, chat_max_steps=3, chat_repair_rounds=2)
    agent = TruncatedAgent([CallStatus.INCOMPLETE, final("לא נמצא הנתון.", "not_found")])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert [o["tool_choice"] for o in agent.options] == ["none", "none"]
    assert m["answer"]["limits_hit"] == ["step_limit"]


def test_no_output_retry_is_started_after_the_step_bound(client, office, monkeypatch):
    setting(monkeypatch, chat_max_steps=1)
    agent = TruncatedAgent([CallStatus.INCOMPLETE, final("אסור להציג")])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "failed" and len(agent.seen) == 1


@pytest.mark.parametrize("elapsed, reserve, retried", [(95, 40, True), (95, 60, True), (115, 40, False), (130, 40, False)])
def test_the_output_retry_leaves_time_to_verify(client, office, monkeypatch, elapsed, reserve, retried):
    from tests.integration.test_chat_inspect import Clock

    setting(monkeypatch, chat_turn_seconds=150, chat_verify_min_seconds=15, chat_verify_reserve_seconds=reserve)
    clock = Clock(monkeypatch)
    agent = TruncatedAgent([CallStatus.INCOMPLETE, final("לא נמצא הנתון.", "not_found")])
    agent.on_step = lambda i: setattr(clock, "offset", elapsed) if i == 0 else None
    m = ask(client, office, monkeypatch, agent)
    assert len(agent.seen) == (2 if retried else 1)
    if retried:
        assert m["status"] == "done", m
        assert 0 < agent.options[1]["timeout"] <= 150 - elapsed - 15
        assert agent.options[1]["tool_choice"] == ("none" if elapsed >= 150 - reserve else None)
    else:
        assert m["status"] == "failed" and m["answer"] is None


def test_cancellation_after_a_truncated_call_prevents_a_retry(office, monkeypatch):
    agent = TruncatedAgent([CallStatus.INCOMPLETE, final("אסור להציג")])
    stopped = False

    def stop(i):
        nonlocal stopped
        stopped = True

    agent.on_step = stop
    with pytest.raises(engine.TurnCancelled) as exc:
        engine.run_turn(office.ctx(), agent, engine.TurnInput("שאלה", [], None, [], {}), lambda *a: None,
                        lambda: stopped)
    assert len(agent.seen) == 1
    assert [u["status"] for u in exc.value.usage if u["purpose"] == "agent"] == ["incomplete"]


def test_an_output_retry_that_uses_the_last_reading_step_must_answer(client, office, monkeypatch):
    setting(monkeypatch, chat_max_steps=4, chat_repair_rounds=2)
    agent = TruncatedAgent([CallStatus.INCOMPLETE, final("לא נמצא הנתון.", "not_found")])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert [o["tool_choice"] for o in agent.options] == [None, "none"]
    assert m["answer"]["limits_hit"] == ["step_limit"]


def test_the_output_budget_is_configurable_and_only_doubled_on_truncation(client, office, monkeypatch):
    setting(monkeypatch, chat_agent_output_tokens=8000)
    agent = TruncatedAgent([CallStatus.INCOMPLETE, final("לא נמצא הנתון.", "not_found")])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert [o["max_output_tokens"] for o in agent.options] == [8000, 16000]


def test_an_unsupported_claim_in_the_output_retry_is_still_removed(client, office, monkeypatch):
    setting(monkeypatch, chat_repair_rounds=0)
    agent = TruncatedAgent([[pages(office.doc, 2)], CallStatus.INCOMPLETE,
                            final('שטח המגרש הוא 990 מ"ר [S1].')])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "done", m
    assert "990" not in m["answer"]["markdown"] and m["answer"]["verification"]["removed"] > 0


def test_a_truncation_consuming_a_repair_slot_keeps_the_global_step_bound(client, office, monkeypatch):
    setting(monkeypatch, chat_max_steps=4, chat_repair_rounds=2)
    wrong = final('שטח המגרש הוא 990 מ"ר [S1].')
    agent = TruncatedAgent([[pages(office.doc, 2)], CallStatus.INCOMPLETE, wrong, wrong, wrong])
    m = ask(client, office, monkeypatch, agent)
    assert len(agent.seen) == 4
    assert m["status"] == "done" and "990" not in m["answer"]["markdown"]
    assert m["answer"]["verification"]["removed"] > 0


def test_a_truncated_repair_keeps_only_the_previously_verified_content(client, office, monkeypatch):
    setting(monkeypatch, chat_max_steps=5, chat_repair_rounds=2)
    mixed = final('שטח המגרש הוא 812 מ"ר [S1].\n\nגובה המבנה הוא 990 מטר [S1].')
    agent = TruncatedAgent([[pages(office.doc, 2)], mixed, CallStatus.INCOMPLETE, CallStatus.INCOMPLETE])
    m = ask(client, office, monkeypatch, agent)
    assert m["status"] == "done" and len(agent.seen) == 4
    assert "812" in m["answer"]["markdown"] and "990" not in m["answer"]["markdown"]
    assert "פלט קטוע" not in m["answer"]["markdown"]
    assert m["answer"]["verification"]["removed"] > 0


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

