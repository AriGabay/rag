"""The turn's time mode in the engine (round 11 U7: R3, R11, R16; KTD2, KTD5).

The models are scripted. What is proven is what the server decides and tells the model: the first user item holds the time
block only when the turn's mode is ``recent`` or ``explicit``, ``resolution.time`` in the diagnostics holds the decision
(mode, source, switch, anchor, requested time), a relative period uses the message's stored anchor, and the time mode
never moves the intent or the conversation's slots (R16) — the same round-10 sequence settles the same intent and leaves
the same slots with the switch on and off. Synthetic data only."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from tests.conftest import login
from tests.integration.test_chat import cloud, new_conversation
from tests.integration.test_chat_intent import APPROACHES, APPROACHES_ANSWER, _focus, _next, _resolution
from tests.integration.test_chat_intent import (
    no_models as no_models,  # noqa: F401 - the autouse inline-run setup
)
from tests.integration.test_chat_intent import setup as setup  # noqa: F401 - the synthetic office
from tests.support.scripted_agent import ScriptedAgent, call, final, intent_part

pytestmark = pytest.mark.db

TIME_BLOCK = "הנחיית זמן"
RENT_QUESTION = "מה דמי השכירות הראויים?"

def rent_agent(setup, **kw) -> ScriptedAgent:
    answer = final("דמי השכירות הראויים נקבעו לפי הנכס [S1].", focus=_focus(setup.rent),
                   claims=[{"text": "דמי השכירות הראויים נקבעו לפי הנכס", "source_ids": ["S1"], "basis": "explicit"}])
    return ScriptedAgent([[call("search", query="דמי השכירות הראויים", document_ids=[setup.rent], limit=None)], answer], **kw)


def ask(client, cid: str, content: str, **extra) -> dict:
    r = client.post(f"/api/chat/conversations/{cid}/messages", json={"content": content, **extra})
    assert r.status_code == 200, r.text
    return client.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()


def diagnostics(client, message: dict) -> dict:
    return client.get(f"/api/chat/messages/{message['id']}/diagnostics").json()["resolution"]


def first_item(agent: ScriptedAgent) -> str:
    return agent.seen[0][0]["content"]


def context_of(office, message: dict) -> dict:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT answer->'context' FROM messages WHERE id = :m"),
                            {"m": UUID(message["id"])}).scalar_one()


def start(client, setup, monkeypatch, agent, *, prefer_recent: bool) -> str:
    cloud(monkeypatch, setup, agent)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    assert client.patch(f"/api/chat/conversations/{cid}", json={"prefer_recent": prefer_recent}).status_code == 200
    return cid


# --- the block and the decision, first message ---------------------------------------------------------------------------

@pytest.mark.parametrize("on", [True, False])
def test_an_explicit_year_overrides_the_switch_and_reaches_the_model_and_the_diagnostics(client, setup, monkeypatch, on):
    agent = rent_agent(setup)
    cid = start(client, setup, monkeypatch, agent, prefer_recent=on)
    m = ask(client, cid, "מה דמי השכירות הראויים ב-2022?")
    assert m["status"] == "done", m
    task = first_item(agent)
    assert TIME_BLOCK in task and "2022" in task
    assert task.index("ההודעה החדשה של המשתמש") < task.index(TIME_BLOCK)  # after the request, not before it
    t = diagnostics(client, m)["time"]
    assert (t["mode"], t["source"], t["prefer_recent"]) == ("explicit", "question", on)
    assert t["requested"]["kind"] == "point" and t["requested"]["start"] == t["requested"]["end"] == "2022"
    assert t["anchor"] == m["time_preference"]["anchor_date"]


def test_with_the_switch_on_and_no_time_words_the_mode_is_recent_and_the_block_says_so(client, setup, monkeypatch):
    agent = rent_agent(setup)
    cid = start(client, setup, monkeypatch, agent, prefer_recent=True)
    m = ask(client, cid, RENT_QUESTION)
    assert m["status"] == "done", m
    task = first_item(agent)
    assert TIME_BLOCK in task and "העדכני" in task
    t = diagnostics(client, m)["time"]
    assert (t["mode"], t["source"], t["prefer_recent"], t["requested"]) == ("recent", "switch", True, None)


def test_with_the_switch_off_and_no_time_words_there_is_no_block_and_the_mode_is_none(client, setup, monkeypatch):
    agent = rent_agent(setup)
    cid = start(client, setup, monkeypatch, agent, prefer_recent=False)
    m = ask(client, cid, RENT_QUESTION)
    assert m["status"] == "done", m
    assert TIME_BLOCK not in first_item(agent)  # R3: no preference reaches the model; the dates stay as the sources give
    t = diagnostics(client, m)["time"]
    assert (t["mode"], t["source"], t["prefer_recent"], t["requested"]) == ("none", "switch", False, None)


def test_a_standard_number_in_the_question_is_not_a_time(client, setup, monkeypatch):
    agent = ScriptedAgent([[call("search", query="גישות השומה", document_ids=None, limit=None)], APPROACHES_ANSWER])
    agent.on("resolve", _resolution(intent=intent_part("professional", "topic", topic="גישות השומה")))
    cid = start(client, setup, monkeypatch, agent, prefer_recent=False)
    m = ask(client, cid, "מה אומר תקן 19 סעיף 2.1 על גישות השומה?")
    assert m["status"] == "done", m
    assert TIME_BLOCK not in first_item(agent)
    assert diagnostics(client, m)["time"]["mode"] == "none"


def test_the_diagnostics_hold_the_decision_also_on_a_turn_that_ends_in_a_clarification(client, setup, monkeypatch):
    agent = ScriptedAgent([final("מה בדיוק לבדוק?", status="clarification", clarification="איזה נכס?")])
    cid = start(client, setup, monkeypatch, agent, prefer_recent=True)
    m = ask(client, cid, "מה השווי בשנה שעברה?")
    t = diagnostics(client, m)["time"]
    assert t["mode"] == "explicit" and t["requested"]["kind"] == "relative"


# --- the anchor (R14) ---------------------------------------------------------------------------------------------------

def test_a_relative_period_uses_the_stored_anchor_and_a_retry_gives_the_same_range(client, setup, monkeypatch):
    first = rent_agent(setup)
    cid = start(client, setup, monkeypatch, first, prefer_recent=True)
    sent = client.post(f"/api/chat/conversations/{cid}/messages", json={"content": "מה דמי השכירות בשנה שעברה?"}).json()
    assistant = client.get(f"/api/chat/messages/{sent['assistant']['id']}").json()
    anchor = assistant["time_preference"]["anchor_date"]
    year = int(anchor[:4])
    assert diagnostics(client, assistant)["time"]["requested"]["start"] == str(year - 1)
    with tenant_tx(setup.system) as conn:  # the message was sent on an earlier day, in an earlier year
        conn.execute(text("UPDATE messages SET time_preference = jsonb_set(time_preference, '{anchor_date}', '\"2024-06-05\"')"
                          " WHERE conversation_id = :c"), {"c": UUID(cid)})
    again = rent_agent(setup)
    _next(monkeypatch, again)
    retried = client.post(f"/api/chat/messages/{sent['assistant']['id']}/retry")
    assert retried.status_code == 200, retried.text
    done = client.get(f"/api/chat/messages/{retried.json()['id']}").json()
    t = diagnostics(client, done)["time"]
    assert t["anchor"] == "2024-06-05" and t["requested"]["start"] == t["requested"]["end"] == "2023"
    assert "2023" in first_item(again)


# --- R16: the time mode moves nothing else ------------------------------------------------------------------------------

def _rent_then(client, setup, monkeypatch, *, prefer_recent: bool, follow_up: str, follow_up_parse: dict) -> tuple:
    first = rent_agent(setup)
    cid = start(client, setup, monkeypatch, first, prefer_recent=prefer_recent)
    one = ask(client, cid, RENT_QUESTION)
    assert one["status"] == "done", one
    second = ScriptedAgent([[call("search", query="היוון ההכנסה", document_ids=[setup.rent], limit=None)],
                            final("היוון ההכנסה נעשה בשיעור שנקבע בשומה [S1].", focus=_focus(setup.rent),
                                  claims=[{"text": "היוון ההכנסה נעשה בשיעור שנקבע בשומה", "source_ids": ["S1"],
                                           "basis": "explicit"}])])
    second.on("resolve", follow_up_parse)
    _next(monkeypatch, second)
    two = ask(client, cid, follow_up)
    assert two["status"] == "done", two
    return one, two, second


def _slots(setup, message: dict) -> dict:
    context = context_of(setup, message)
    return {k: v for k, v in context.items() if k not in ("sources",)}


def _signature(client, setup, message: dict) -> dict:
    res = diagnostics(client, message)
    return {"intent": res["intent"]["value"], "decisions": {k: v for k, v in res["decisions"].items()
                                                              if k in ("intent", "entity", "first_message")},
            "slots": {k: v for k, v in _slots(setup, message).items() if k in ("topic", "property", "params")},
            "request": message["answer"]["request"]["intent"]["value"]}


@pytest.mark.parametrize("follow_up", [
    "ואיך מהוונים את ההכנסה?",
    "ואיך מהוונים את ההכנסה בשומה מיום 15/04/2024?",   # a date is time, not the property (R16)
    "ואיך מהוונים את ההכנסה בשנה שעברה?",
])
def test_the_same_sequence_has_the_same_intent_and_slots_with_the_switch_on_and_off(client, setup, monkeypatch, follow_up):
    parse = _resolution(relation="same_datum", intent=intent_part("property", "conversation_property"),
                        document_ids=[setup.rent])
    signatures = {}
    for on in (True, False):
        _, two, _agent = _rent_then(client, setup, monkeypatch, prefer_recent=on, follow_up=follow_up, follow_up_parse=parse)
        signatures[on] = _signature(client, setup, two)
        assert signatures[on]["request"] == "property"
        assert signatures[on]["slots"]["property"], signatures[on]  # the property slot is still the rent document
    assert signatures[True] == signatures[False]


def test_an_appraisal_date_in_the_question_is_a_point_and_leaves_the_property_slot_unchanged(client, setup, monkeypatch):
    parse = _resolution(relation="same_datum", intent=intent_part("property", "conversation_property"),
                        document_ids=[setup.rent])
    one, two, agent = _rent_then(client, setup, monkeypatch, prefer_recent=False,
                                 follow_up="ואיך מהוונים את ההכנסה בשומה מיום 15/04/2024?", follow_up_parse=parse)
    t = diagnostics(client, two)["time"]
    assert (t["mode"], t["source"]) == ("explicit", "question")
    assert (t["requested"]["kind"], t["requested"]["start"], t["requested"]["precision"]) == ("point", "2024-04-15", "day")
    assert TIME_BLOCK in first_item(agent)
    assert _slots(setup, two)["property"] == _slots(setup, one)["property"]


def test_the_time_block_does_not_carry_to_the_next_turn(client, setup, monkeypatch):
    parse = _resolution(relation="same_datum", intent=intent_part("property", "conversation_property"),
                        document_ids=[setup.rent])
    first = rent_agent(setup)
    cid = start(client, setup, monkeypatch, first, prefer_recent=False)
    one = ask(client, cid, "מה דמי השכירות הראויים ב-2022?")
    assert diagnostics(client, one)["time"]["mode"] == "explicit" and TIME_BLOCK in first_item(first)
    second = rent_agent(setup)
    second.on("resolve", parse)
    _next(monkeypatch, second)
    two = ask(client, cid, "ומה היה היוון ההכנסה?")
    assert two["status"] == "done", two
    assert TIME_BLOCK not in first_item(second)  # nothing of the earlier question's time reaches this turn
    assert diagnostics(client, two)["time"]["mode"] == "none"


def test_a_professional_question_after_a_property_turn_is_unchanged_by_the_switch(client, setup, monkeypatch):
    third_parse = _resolution(intent=intent_part("professional", "topic", topic="גישות השומה"))
    seen = {}
    for on in (True, False):
        first = rent_agent(setup)
        cid = start(client, setup, monkeypatch, first, prefer_recent=on)
        assert ask(client, cid, RENT_QUESTION)["status"] == "done"
        third = ScriptedAgent([[call("search", query="גישות השומה", document_ids=None, limit=None)], APPROACHES_ANSWER])
        third.on("resolve", third_parse)
        _next(monkeypatch, third)
        m = ask(client, cid, APPROACHES)
        assert m["status"] == "done", m
        res = diagnostics(client, m)
        assert "lookup" not in res and "entity" not in res["decisions"]
        assert m["answer"]["request"]["document_ids"] == []
        seen[on] = (res["intent"]["value"], res["decisions"]["intent"], "זו שאלה מקצועית כללית" in first_item(third),
                    "הנתון שבמרכז השיחה" in first_item(third))
    assert seen[True] == seen[False] == ("professional", "professional", True, False)
