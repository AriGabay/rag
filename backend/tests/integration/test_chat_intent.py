"""A general professional question is answered as one: no lookup, no choice between properties, whatever word its
text shares with the titles of the office's documents (round 10 U6; AE1, KTD1-KTD3, KTD7).

The reported conversation had a property in its centre (rent, then capitalisation) and then asked a general question
that used the word every appraisal's title holds; the server looked the word up, found three appraisals and asked
which was meant. The models are scripted; these tests prove what the server decides. Synthetic documents and invented
places only."""

from __future__ import annotations

import time

import pytest

from app.chat import contexts, entities
from app.platform import pipeline
from app.providers.llm import CallStatus
from tests.conftest import login
from tests.factories import make_group, make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_coverage import add_doc
from tests.support.scripted_agent import ScriptedAgent, call, final, intent_part
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

APPROACHES = "מה הן שלושת גישות השומה הבסיסיות ?"


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    contexts._CACHE.clear()


@pytest.fixture
def setup(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    make_group(a, "קבוצה 2")
    g = a.default_group_id
    a.rent = add_doc(a, g, "דירת מגורים רחוב התמר 9 עיר-ב (סינתטי)", [
        "דמי השכירות הראויים נקבעו לפי הנכס ברחוב התמר 9. היוון ההכנסה נעשה בשיעור שנקבע בשומה."], "1" * 64)
    add_doc(a, g, "שומה מקרקעין רחוב הדקל 5 עיר-א (סינתטי)", ["שומה לפי גישת ההשוואה ולפי גישת ההכנסות."], "2" * 64)
    add_doc(a, g, "תל-סינתטי שומה", ["השומה נערכה לפי גישת העלות בלבד."], "3" * 64)
    add_doc(a, g, "חלקה 7 בגוש שומה 1234 (סינתטי)", ["שומה לפי גישת ההכנסות. גוש 1234 חלקה 7."], "4" * 64)
    return a


def _resolution(**kw) -> dict:
    return {"relation": "new_question", "scope": "entity", "standalone_question": "?", "changed_fields": [],
            "metric_kind": "unknown", "unit": "unknown", "scale": "unknown", "period": "unknown", "area_basis": "",
            "vat": "unknown", "subject": "", "document_ids": [], "ambiguity": ""} | kw


def _focus(doc: str) -> dict:
    return {"metric_as_written": "דמי שכירות ראויים", "metric_kind": "rent", "unit": "ILS", "period": "month",
            "area_basis": "", "vat": "unknown", "subject": "התמר 9", "value_role": "appraiser_determination",
            "document_ids": [doc]}


def _next(monkeypatch, agent) -> None:
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)


def _diagnostics(client, message: dict) -> dict:
    return client.get(f"/api/chat/messages/{message['id']}/diagnostics").json()


def _rent_then_capitalisation(client, setup, monkeypatch) -> str:
    first = ScriptedAgent([[call("search", query="דמי השכירות הראויים", document_ids=[setup.rent], limit=None)],
                           final("דמי השכירות הראויים נקבעו לפי הנכס [S1].", focus=_focus(setup.rent),
                                 claims=[{"text": "דמי השכירות הראויים נקבעו לפי הנכס", "source_ids": ["S1"],
                                          "basis": "explicit"}])])
    cloud(monkeypatch, setup, first)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    assert send(client, cid, "מה דמי השכירות הראויים?")["status"] == "done"
    second = ScriptedAgent([[call("search", query="היוון ההכנסה", document_ids=[setup.rent], limit=None)],
                            final("היוון ההכנסה נעשה בשיעור שנקבע בשומה [S1].", focus=_focus(setup.rent),
                                  claims=[{"text": "היוון ההכנסה נעשה בשיעור שנקבע בשומה", "source_ids": ["S1"],
                                           "basis": "explicit"}])])
    second.on("resolve", _resolution(relation="same_datum", intent=intent_part("property", "conversation_property"),
                                     document_ids=[setup.rent]))
    _next(monkeypatch, second)
    assert send(client, cid, "ואיך מהוונים את ההכנסה?")["status"] == "done"
    return cid


APPROACHES_ANSWER = final("שלוש הגישות הן גישת ההשוואה, גישת ההכנסות וגישת העלות [S1].",
                          claims=[{"text": "שלוש הגישות הן גישת ההשוואה, גישת ההכנסות וגישת העלות",
                                   "source_ids": ["S1"], "basis": "explicit"}])


@pytest.mark.parametrize("parse, settled", [
    (_resolution(intent=intent_part("professional", "topic", topic="גישות השומה")), "professional"),
    (_resolution(), "unknown"),  # a script with no intent part: the evidence-gated path alone decides
    (_resolution(intent=intent_part("property", "named_entity")), "property"),
])
def test_the_approaches_question_after_a_property_turn_is_a_model_answer_without_a_lookup(client, setup, monkeypatch,
                                                                                           parse, settled):
    cid = _rent_then_capitalisation(client, setup, monkeypatch)
    third = ScriptedAgent([[call("search", query="גישות השומה", document_ids=None, limit=None)], APPROACHES_ANSWER])
    third.on("resolve", parse)
    _next(monkeypatch, third)
    m = send(client, cid, APPROACHES)
    assert m["status"] == "done", m
    a = m["answer"]
    assert a["status"] == "answered" and "גישת ההשוואה" in a["markdown"]  # a model-answered turn, not a choice
    res = _diagnostics(client, m)["resolution"]
    assert "lookup" not in res and "entity" not in res["decisions"]
    assert res["decisions"]["intent"] == settled and res["intent"]["value"] == settled
    assert a["request"]["intent"]["value"] == settled and not a["request"]["candidates"]
    task = third.seen[0][0]["content"]
    if settled == "professional":
        assert a["request"]["document_ids"] == []
        assert "הנתון שבמרכז השיחה" not in task and "זו שאלה מקצועית כללית" in task  # KTD7: no focus block
    else:
        assert "זו שאלה מקצועית כללית" not in task


# --- the first message (KTD3) -------------------------------------------------------------------------------------

UNIT_VALUES = ("1,100,000", "2,200,000", "3,300,000")


@pytest.fixture
def units(setup):
    """Three appraisals of one file whose stored units carry no title: the one word that tells them apart is in their
    passages (here "גישות", held by the second only), so a first message that uses it is pinned to that unit by its
    passages — provisionally."""
    setup.doc, setup.ver = unit_file(setup, setup.default_group_id, "קובץ שומות סינתטי ללא כותרות", [
        {"blocks": ["מתחם הנרקיס, שומת מקרקעין", f"שווי הנכס {UNIT_VALUES[0]} ₪."]},
        {"blocks": ["מתחם הצפצפה, שומת מקרקעין", f"גישות ההערכה בשומה זו שלוש. שווי הנכס {UNIT_VALUES[1]} ₪."]},
        {"blocks": ["מתחם הדולב, שומת מקרקעין", f"שווי הנכס {UNIT_VALUES[2]} ₪."]}], sha="u" * 64)
    return setup


GENERAL = "מה הן שלוש גישות ההערכה?"


def _general_answer() -> ScriptedAgent:
    return ScriptedAgent([[call("search", query="גישות ההערכה", document_ids=None, limit=None, unit=None)],
                          final("אין מידע.", "not_found")])


def test_the_passages_pin_is_the_provisional_one(units):
    found = entities.first_message(units.ctx(), GENERAL)
    assert found is not None and found.unit["unit_no"] == 2 and found.ambiguity is None


@pytest.mark.parametrize("value, pinned", [("professional", False), ("unknown", False), ("property", True),
                                           ("mixed", True), ("document", True)])
def test_a_first_message_pin_from_passages_waits_for_the_intent(client, units, monkeypatch, value, pinned):
    agent = ScriptedAgent([[call("search", query="גישות ההערכה", document_ids=None, limit=None, unit=None)],
                           final("אין מידע.", "not_found")], intent=value)
    cloud(monkeypatch, units, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), GENERAL)
    assert m["status"] == "done", m
    task = agent.seen[0][0]["content"]
    assert ("unit=U1" in task) is pinned, task
    res = _diagnostics(client, m)["resolution"]
    assert res["decisions"]["intent"] == value
    assert res["decisions"]["first_message"].startswith("provisional, " + ("applied" if pinned else "dropped"))
    assert ("זו שאלה מקצועית כללית" in task) is (value == "professional")


def test_a_first_message_whose_analysis_times_out_drops_the_pin_and_records_the_wait(client, units, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_intent_wait_seconds", 0.3)

    def slow(_input: str):
        time.sleep(1.5)  # the analysis answers after the wait: the intent was unknown while the tools ran
        return []

    agent = ScriptedAgent([[call("search", query="גישות ההערכה", document_ids=None, limit=None, unit=None)],
                           final("אין מידע.", "not_found")], request=slow, intent="property")
    cloud(monkeypatch, units, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), GENERAL)
    assert m["status"] == "done", m
    assert "unit=U1" not in agent.seen[0][0]["content"]  # the pin was dropped
    d = _diagnostics(client, m)
    assert d["resolution"]["decisions"]["first_message"] == "provisional, dropped: intent unknown"
    assert d["timing"]["waits"]["intent"] >= 0.25  # the wait is in the turn's timing


def test_a_first_message_whose_analysis_fails_is_unknown_and_drops_the_pin(client, units, monkeypatch):
    agent = ScriptedAgent([[call("search", query="גישות ההערכה", document_ids=None, limit=None, unit=None)],
                           final("אין מידע.", "not_found")], request=CallStatus.TIMEOUT)
    cloud(monkeypatch, units, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), GENERAL)
    assert m["status"] == "done", m
    assert "unit=U1" not in agent.seen[0][0]["content"]
    res = _diagnostics(client, m)["resolution"]
    assert res["decisions"]["intent"] == "unknown"


def test_the_analysis_is_collected_before_the_first_steps_tools_run(client, setup, monkeypatch):
    seen: list[bool] = []
    agent = ScriptedAgent([[call("search", query="גישות השומה", document_ids=None, limit=None)], APPROACHES_ANSWER],
                          intent="professional")

    def on_step(index: int) -> None:
        if index == 1:  # the second step: the usage of the analysis is already in (collected after step one)
            seen.append(any(c.schema.__name__ == "RequestAnalysis" for c in agent.calls))

    agent.on_step = on_step
    cloud(monkeypatch, setup, agent)
    login(client, "admin-a@example.test")
    m = send(client, new_conversation(client), APPROACHES)
    assert m["status"] == "done" and seen == [True]
    res = _diagnostics(client, m)["resolution"]
    assert res["intent"]["value"] == "professional" and res["decisions"]["intent"] == "professional"
    assert [u["purpose"] for u in m["usage"]][:2] == ["agent", "request"]


# --- the follow-up gate (KTD3) ------------------------------------------------------------------------------------

def test_a_professional_follow_up_whose_words_are_unique_to_one_unit_pins_no_unit_and_asks_nothing(client, units,
                                                                                                    monkeypatch):
    first = ScriptedAgent([final("אין מידע.", "not_found")])
    cloud(monkeypatch, units, first)
    login(client, "admin-a@example.test")
    cid = new_conversation(client)
    assert send(client, cid, "שלום")["status"] == "done"
    second = ScriptedAgent([[call("search", query="גישות ההערכה", document_ids=None, limit=None, unit=None)],
                            final("אין מידע.", "not_found")])
    second.on("resolve", _resolution(intent=intent_part("professional", "topic", topic="גישות")))
    _next(monkeypatch, second)
    m = send(client, cid, GENERAL)
    assert m["status"] == "done" and m["answer"]["status"] != "clarification", m
    assert "unit=U1" not in second.seen[0][0]["content"]
    # the same words from a parse that says property, with no evidence beyond the passages: still no pin
    third = ScriptedAgent([[call("search", query="גישות ההערכה", document_ids=None, limit=None, unit=None)],
                           final("אין מידע.", "not_found")])
    third.on("resolve", _resolution(intent=intent_part("property")))
    _next(monkeypatch, third)
    m = send(client, cid, GENERAL)
    assert m["status"] == "done" and "unit=U1" not in third.seen[0][0]["content"]
