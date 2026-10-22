"""Conversation slots, a pending clarification that lives one message, and ordinals that bind to the previous answer's
items (round 10 U7; R10-R15, AE2-AE7, KTD4-KTD7).

Each answer stores the conversation's slots apart in ``answer.context`` (server-only): the professional topic with the
items its answer listed, the selected property with its task parameters, the sources, and the pending clarification.
The next turn reads them back from the database, so a professional question in the middle of a property conversation
leaves the property where it was, and "השנייה" means the second item of the list the answer printed. The models are
scripted: these tests prove what the server decides and stores. Synthetic documents and invented places only."""

from __future__ import annotations

import json
from uuid import UUID

import pytest
from sqlalchemy import text

from app.chat import api as chat_api
from app.chat import contexts
from app.db import tenant_tx
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_chat import cloud, new_conversation, send
from tests.integration.test_chat_calculation import RISE, _clarification_turn
from tests.integration.test_chat_calculation import office as office  # noqa: F401 - the calculation fixture
from tests.integration.test_chat_coverage import add_doc
from tests.integration.test_chat_units import R9B, SIMILAR_QUESTION, ingest
from tests.support.scripted_agent import ScriptedAgent, call, final, intent_part
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

SECRET_SUBJECT = "נכס-סודי-4471"  # the subject text of a focus: it must never outlive a revoked document


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    contexts._CACHE.clear()


GUIDE = ["שלוש גישות השומה הן גישת ההשוואה, גישת ההכנסות וגישת העלות.",
         "גישת ההשוואה מבוססת על עסקאות דומות בסביבת הנכס.", "גישת ההכנסות מהוונת את ההכנסה של הנכס.",
         "גישת העלות מחשבת את עלות הבנייה של הנכס."]
UNITS = ["מתחם הנרקיס", "מתחם הצפצפה", "מתחם הדולב"]
VALUES = ["1,100,000", "2,200,000", "3,300,000"]
APPROACHES = ["גישת ההשוואה", "גישת ההכנסות", "גישת העלות"]
LIST_ANSWER = ("שלוש גישות השומה הן:\n1. **גישת ההשוואה** — השוואה לעסקאות דומות [S1]\n"
               "2. **גישת ההכנסות** — היוון ההכנסה [S1]\n3. **גישת העלות** — חישוב עלות הבנייה [S1]")


@pytest.fixture
def world(db, client, monkeypatch):
    a = make_office(db, "משרד א", "admin-a@example.test")
    g = a.default_group_id
    a.rent = add_doc(a, g, "דירת מגורים רחוב התמר 9 עיר-ב (סינתטי)", [
        "דמי השכירות הראויים נקבעו לפי הנכס ברחוב התמר 9.", "היוון ההכנסה נעשה בשיעור שנקבע בשומה."], "1" * 64)
    a.guide = add_doc(a, g, "מדריך שמאות כללי (סינתטי)", GUIDE, "2" * 64)
    a.jasmine = add_doc(a, g, "דירה ברחוב היסמין 3 עיר-ג (סינתטי)", ["דמי השכירות נקבעו ברחוב היסמין 3."], "3" * 64)
    a.file, _ = unit_file(a, g, "קובץ מתחמים סינתטי", [
        {"title": t, "aliases": [t], "blocks": [f"{t}, שומת מקרקעין", f"שווי הנכס {v} ₪."]}
        for t, v in zip(UNITS, VALUES, strict=True)], sha="u" * 64)
    cloud(monkeypatch, a, ScriptedAgent([]))
    login(client, "admin-a@example.test")
    a.client, a.cid, a.monkeypatch = client, new_conversation(client), monkeypatch
    return a


def _resolution(**kw) -> dict:
    return {"relation": "new_question", "scope": "entity", "standalone_question": "?", "changed_fields": [],
            "metric_kind": "unknown", "unit": "unknown", "scale": "unknown", "period": "unknown", "area_basis": "",
            "vat": "unknown", "subject": "", "document_ids": [], "ambiguity": ""} | kw


def _focus(doc: str, subject: str = "התמר 9", kind: str = "rent", **kw) -> dict:
    return {"metric_as_written": "דמי שכירות ראויים", "metric_kind": kind, "unit": "ILS", "period": "month",
            "area_basis": "", "vat": "unknown", "subject": subject, "value_role": "appraiser_determination",
            "document_ids": [doc]} | kw


def _claims(*texts: str) -> list[dict]:
    return [{"text": t, "source_ids": ["S1"], "basis": "explicit"} for t in texts]


def _turn(world, question: str, steps: list, resolve: dict | None = None, **kw) -> tuple[dict, ScriptedAgent]:
    """One turn: the agent that answers it (and, on a follow-up, the resolve call's parse), the finished message."""
    agent = ScriptedAgent(steps, **kw)
    if resolve is not None:
        agent.on("resolve", resolve)
    world.monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    m = send(world.client, world.cid, question)
    assert m["status"] == "done", m
    return m, agent


def _rent_turn(world) -> dict:
    """The conversation's first turn: a property question, answered with the rent of the first document in focus."""
    m, _ = _turn(world, "מה דמי השכירות הראויים?", [
        [call("search", query="דמי השכירות הראויים", document_ids=[world.rent], limit=None)],
        final("דמי השכירות הראויים נקבעו לפי הנכס [S1].", focus=_focus(world.rent, SECRET_SUBJECT),
              claims=_claims("דמי השכירות הראויים נקבעו לפי הנכס"))], intent="property")
    return m


def _unit_turn(world) -> dict:
    """A first turn that names a unit of the file of several by its complete name: the property is that unit."""
    m, _ = _turn(world, "מה שווי הנכס במתחם הדולב?", [
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")],  # the candidate the model was offered
        final(f"שווי הנכס הוא {VALUES[2]} ₪ [S1].",
              focus=_focus(world.file, "מתחם הדולב", "value", unit="ILS", units=["U1"]),
              claims=_claims(f"שווי הנכס הוא {VALUES[2]} ₪"))], intent="property")
    return m


def _professional_list(world, first: bool = False) -> tuple[dict, ScriptedAgent]:
    """A professional answer that lists the three approaches: a first turn, or a follow-up whose parse says
    ``professional``."""
    steps = [[call("search", query="גישות השומה", document_ids=[world.guide], limit=None)],
             final(LIST_ANSWER, claims=_claims("שלוש גישות השומה הן גישת ההשוואה, גישת ההכנסות וגישת העלות"))]
    question = "מה הן שלוש גישות השומה?"
    if first:
        return _turn(world, question, steps, None, intent="professional")
    return _turn(world, question, steps, _resolution(
        standalone_question=question, intent=intent_part("professional", "topic", topic="גישות השומה")))


def _answer_row(world, m: dict) -> dict:
    with tenant_tx(world.system) as conn:
        return conn.execute(text("SELECT answer FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()


def _labels(ctx: dict) -> list[str]:
    """The labels of the newest list a context carries (round 22 U12: lists, not ``topic.items``)."""
    return [i["label"] for i in ctx["lists"][0]["items"]]


def _context(world, m: dict) -> dict:
    return _answer_row(world, m)["context"]


def _decisions(world, m: dict) -> dict:
    return world.client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["resolution"]["decisions"]


def _next_input(world, question: str = "הודעה הבאה"):
    """The input of the next turn, rebuilt from the database alone (what a reload or another process would read)."""
    with tenant_tx(world.ctx()) as conn:
        uid = conn.execute(text(
            "INSERT INTO messages (office_id, conversation_id, user_id, role, content, status, created_at) VALUES"
            " (app_office(), :c, :u, 'user', :t, 'done', clock_timestamp()) RETURNING id"),
            {"c": world.cid, "u": world.admin_id, "t": question}).scalar_one()
        inp, _ = chat_api._turn_input(conn, world.ctx(), UUID(world.cid), uid)
    return inp


def _task(agent: ScriptedAgent) -> str:
    return agent.seen[0][0]["content"]


def _resolve_input(agent: ScriptedAgent) -> str:
    return next(c.input for c in agent.calls if c.purpose == "resolve")


# --- the property survives a professional turn (AE4, AE3, bug 1) ---------------------------------------------------------


def test_ae4_a_professional_turn_leaves_the_property_and_its_unit_for_the_next_turn(world):
    first = _unit_turn(world)
    assert first["answer"]["focus"]["document_ids"] == [world.file]
    m, _ = _professional_list(world)
    assert m["answer"]["focus"] is None and m["answer"]["request"]["document_ids"] == []  # released (KTD7)
    slot = _context(world, m)["property"]
    assert slot["document_ids"] == [world.file] and slot["located_unit"]["display"] == "מתחם הדולב"
    assert slot["subject"] == "מתחם הדולב"
    assert _context(world, m)["params"]["metric_kind"] == "value"  # the task parameters are dormant with it
    third, agent = _turn(world, "ומה השווי שם?", [
        [call("search", query="שווי הנכס", document_ids=None, limit=None, unit="U1")],
        final(f"שווי הנכס הוא {VALUES[2]} ₪ [S1].", claims=_claims(f"שווי הנכס הוא {VALUES[2]} ₪"))],
        _resolution(relation="same_datum", intent=intent_part("property", "conversation_property"),
                    standalone_question="מה השווי במתחם הדולב?"))
    req = third["answer"]["request"]
    assert req["document_ids"] == [world.file] and req["located_unit"]["display"] == "מתחם הדולב"
    assert "מתחם הדולב" in _task(agent)


def test_a_professional_answer_without_a_focus_no_longer_erases_the_property_for_the_next_turn(world):
    _rent_turn(world)
    m, _ = _professional_list(world)
    assert m["answer"].get("focus") is None
    inp = _next_input(world)
    assert inp.focus is not None and inp.focus["document_ids"] == [world.rent]
    assert inp.focus["subject"] == SECRET_SUBJECT and inp.focus["metric_kind"] == "rent"


def test_ae3_how_to_apply_it_to_this_property_restores_the_property_beside_the_topic(world):
    _rent_turn(world)
    _professional_list(world)
    m, agent = _turn(world, "איך מיישמים אותה בנכס הזה?", [
        [call("search", query="גישת ההשוואה", document_ids=[world.rent], limit=None)],
        final("הגישה מיושמת על הנכס ברחוב התמר 9 [S1].", claims=_claims("הגישה מיושמת על הנכס ברחוב התמר 9"))],
        _resolution(intent=intent_part("mixed", "topic"), standalone_question="איך מיישמים את הגישה בנכס הזה?"))
    req = m["answer"]["request"]
    assert req["intent"]["value"] == "mixed" and req["document_ids"] == [world.rent]
    assert req["topic"] == "גישות השומה"  # the pronoun is bound to the topic slot
    assert _decisions(world, m)["documents"].startswith("reinstated")
    assert "גישות השומה" in _task(agent)
    ctx = _context(world, m)
    assert ctx["property"]["document_ids"] == [world.rent] and ctx["topic"]["text"] == "גישות השומה"
    assert _labels(ctx) == APPROACHES  # the list is still the one the answer printed


def test_ae5_a_metric_change_keeps_the_property_and_a_named_address_replaces_it(world):
    _rent_turn(world)
    m, _ = _turn(world, "ומה השווי?", [
        [call("search", query="שווי", document_ids=[world.rent], limit=None)],
        final("השווי נקבע לפי הנכס [S1].", focus=_focus(world.rent, SECRET_SUBJECT, "value"),
              claims=_claims("השווי נקבע לפי הנכס"))],
        _resolution(relation="metric_change", intent=intent_part("property", "conversation_property"),
                    changed_fields=[{"field": "metric_kind", "user_words": "השווי"}], metric_kind="value"))
    assert m["answer"]["request"]["document_ids"] == [world.rent]
    assert _context(world, m)["property"]["document_ids"] == [world.rent]
    m2, _ = _turn(world, "ובהיסמין 3?", [
        [call("search", query="דמי השכירות", document_ids=[world.jasmine], limit=None)],
        final("דמי השכירות נקבעו ברחוב היסמין 3 [S1].", focus=_focus(world.jasmine, "היסמין 3", "value"),
              claims=_claims("דמי השכירות נקבעו ברחוב היסמין 3"))],
        _resolution(intent=intent_part("property", "named_entity"),
                    changed_fields=[{"field": "subject", "user_words": "היסמין 3"}], subject="היסמין 3"))
    assert m2["answer"]["request"]["document_ids"] == [world.jasmine]
    assert _context(world, m2)["property"]["document_ids"] == [world.jasmine]


# --- ordinals bind to the previous answer's items (AE2, KTD6, bug 4) ----------------------------------------------------


def test_the_items_of_a_numbered_answer_are_stored_as_labels(world):
    m, _ = _professional_list(world, first=True)
    ctx = _context(world, m)
    (entry,) = ctx["lists"]
    assert _labels(ctx) == APPROACHES and entry["message_id"] == m["id"] and entry["id"] == f"{m['id']}#1" and entry["total"] == 3
    assert _context(world, m)["version"] == 2 and _context(world, m)["pending"] is None
    assert {s["document_id"] for s in _context(world, m)["sources"]} == {world.guide}


def test_ae2_the_second_approach_names_the_second_item_of_the_list(world):
    _professional_list(world, first=True)
    m, agent = _turn(world, "הסבר את הגישה השנייה", [
        [call("search", query="גישת ההכנסות", document_ids=[world.guide], limit=None)],
        final("גישת ההכנסות מהוונת את ההכנסה של הנכס [S1].", claims=_claims("גישת ההכנסות מהוונת את ההכנסה"))],
        _resolution(standalone_question="הסבר את הגישה השנייה",
                    intent=intent_part("professional", "prior_answer_item", item=2)))
    req = m["answer"]["request"]
    assert req["intent"]["value"] == "professional" and req["intent"]["item"] == 2
    assert req["intent"]["item_label"] == "גישת ההכנסות" and "גישת ההכנסות" in req["standalone_question"]
    assert req["topic"] == "גישת ההכנסות" and req["document_ids"] == []
    assert _decisions(world, m)["item"].startswith("bound: 2")
    task = _task(agent)
    assert "גישת ההכנסות" in task and "זו שאלה מקצועית כללית" in task
    assert "2. גישת ההכנסות" in _resolve_input(agent)  # the model was shown the items of the previous answer
    assert _context(world, m)["topic"]["text"] == "גישת ההכנסות"
    assert _labels(_context(world, m)) == APPROACHES  # a later answer lists none: the list persists


def test_an_ordinal_with_no_list_in_the_previous_answer_is_rejected_recorded_and_answered_as_asked(world):
    _turn(world, "מה זה שווי שוק?", [
        [call("search", query="גישות", document_ids=[world.guide], limit=None)],
        final("גישת ההשוואה מבוססת על עסקאות דומות [S1].", claims=_claims("גישת ההשוואה מבוססת על עסקאות"))],
        None, intent="professional")
    m, agent = _turn(world, "הסבר את הגישה השנייה", [
        [call("search", query="גישה", document_ids=[world.guide], limit=None)],
        final("אפשר להסביר רק גישה שמצוינת [S1].", claims=_claims("אפשר להסביר רק גישה"))],
        _resolution(standalone_question="הסבר את הגישה השנייה",
                    intent=intent_part("professional", "prior_answer_item", item=2)))
    req = m["answer"]["request"]
    assert req["intent"]["item"] is None and req["intent"]["reference"] == "none"
    assert _decisions(world, m)["item"].startswith("rejected") and "list" in _decisions(world, m)["item"]
    assert "לא ניתנה רשימה" in _task(agent)  # the answer says no list was given


def test_a_digit_that_is_not_the_item_number_and_an_item_past_the_list_are_rejected(world):
    _professional_list(world, first=True)
    for message, item in [("ומה היה השווי ב-2024?", 2), ("הסבר את הגישה הרביעית", 4), ("הסבר אותה", 2)]:
        m, _ = _turn(world, message, [final("בדיקה.", "not_found")], _resolution(
            standalone_question=message, intent=intent_part("professional", "prior_answer_item", item=item)))
        assert m["answer"]["request"]["intent"]["item"] is None, message
        assert _decisions(world, m)["item"].startswith("rejected"), message


def test_items_persist_the_second_and_then_the_third_bind_to_the_same_list(world):
    _professional_list(world, first=True)
    labels = []
    for message, item in [("הסבר את השנייה", 2), ("ואת השלישית", 3)]:
        m, _ = _turn(world, message, [final("הסבר.", "not_found")], _resolution(
            standalone_question=message, intent=intent_part("professional", "prior_answer_item", item=item)))
        labels.append(m["answer"]["request"]["intent"]["item_label"])
    assert labels == ["גישת ההכנסות", "גישת העלות"]


def test_an_ordinal_that_fits_a_numbered_answer_and_a_listing_of_the_previous_turn_is_told_to_the_model_as_two_readings(world):
    # the answer shows the documents of the listing as well as the approaches: two lists the user saw
    listed = ("השומות שלנו ברשימה [S1]: דירת מגורים רחוב התמר 9 עיר-ב (סינתטי); מדריך שמאות כללי (סינתטי); דירה "
              "ברחוב היסמין 3 עיר-ג (סינתטי). הגישות שהן משתמשות בהן:\n1. **גישת ההשוואה**\n2. **גישת ההכנסות**\n"
              "3. **גישת העלות**")
    first, _ = _turn(world, "אילו שומות יש לנו ואילו גישות הן משתמשות בהן?", [
        [call("list_documents", query="סינתטי", page=None)],
        final(listed, scope="set", scope_query="סינתטי")], None, intent="set")
    assert [s["kind"] for s in first["answer"]["sources"]] == ["listing"]
    m, agent = _turn(world, "השנייה", [final("התכוונת לגישה השנייה: גישת ההכנסות.", "not_found")], _resolution(
        standalone_question="השנייה", intent=intent_part("professional", "prior_answer_item", item=2)))
    a = m["answer"]
    # round 15 U6 (KTD1, R2): the two readings are the model's to weigh - the server asks nothing in its place
    assert a["status"] != "clarification" and len(agent.seen) == 1
    assert "גישת ההכנסות" in _task(agent) and "ברשימת המסמכים" in _task(agent)
    assert _decisions(world, m)["item"].startswith("ambiguous")
    assert _context(world, m)["pending"] is None  # the reply is a new message, not a bound answer


def test_a_listing_the_answer_did_not_show_is_no_second_reading_of_an_ordinal(world):
    # round 10 real-model run on 8548fb2: find_standards' listing, read but not presented, made "השנייה" ask between the
    # second approach and a document the user never saw
    first, _ = _turn(world, "אילו גישות יש?", [
        [call("list_documents", query="סינתטי", page=None)],
        final("הגישות:\n1. **גישת ההשוואה**\n2. **גישת ההכנסות**\n3. **גישת העלות** [S1]", scope="set",
              scope_query="סינתטי")], None, intent="set")
    m, _ = _turn(world, "השנייה", [final("גישת ההכנסות מהוונת הכנסה.", "not_found")], _resolution(
        standalone_question="השנייה", intent=intent_part("professional", "prior_answer_item", item=2)))
    assert m["answer"]["status"] != "clarification"
    assert _decisions(world, m)["item"].startswith("bound")


# --- a pending clarification lives one message (AE6, AE7, KTD5, bugs 2, 3) ----------------------------------------------


def _clarification(world) -> dict:
    """The model's question between the two similar units of R9B, offering both (the cards it was shown, U1 and U2)."""
    doc, _ = ingest(world, R9B)
    asked = "בקובץ שני מתחמים: מתחם הגפן א ומתחם הגפן ב. לאיזה התכוונת?"
    m, agent = _turn(world, SIMILAR_QUESTION, [final(asked, "clarification", clarification=asked,
                                                        focus=_focus(doc, "", "unknown", units=["U1", "U2"]))],
                     None, intent="property")
    assert m["answer"]["status"] == "clarification" and len(agent.seen) == 1
    return m


def test_a_model_clarification_stores_the_units_it_offered_as_the_pending_slot(world):
    m = _clarification(world)
    pending = _context(world, m)["pending"]
    assert pending["kind"] == "candidates" and pending["asked_at"]
    assert [c["unit"]["unit_no"] for c in pending["payload"]["candidates"]] == [1, 2]
    inp = _next_input(world)
    assert [c["unit"]["unit_no"] for c in inp.candidates] == [1, 2]


def test_ae6_a_general_question_after_a_clarification_is_answered_and_the_clarification_expires(world):
    _clarification(world)
    m, _ = _turn(world, "מה זה שווי שוק?", [
        [call("search", query="שווי שוק", document_ids=[world.guide], limit=None)],
        final("שווי שוק נקבע לפי עסקאות דומות [S1].", claims=_claims("שווי שוק נקבע לפי עסקאות דומות"))],
        _resolution(standalone_question="מה זה שווי שוק?",
                    intent=intent_part("professional", "topic", topic="שווי שוק")))
    assert m["answer"]["status"] == "answered" and not m["answer"]["request"]["candidates"]
    assert _decisions(world, m)["pending"].startswith("expired")
    assert _context(world, m)["pending"] is None
    m2, agent = _turn(world, "השנייה", [final("בדיקה.", "not_found")], _resolution(standalone_question="השנייה"))
    assert m2["answer"]["status"] != "clarification"
    assert m2["answer"]["request"]["document_ids"] == [] and not m2["answer"]["request"]["candidates"]
    assert "שאלת הבהרה" not in _resolve_input(agent)  # its candidates are out of the next resolve input


@pytest.mark.parametrize("relation", ["clarification_answer", "new_question"])
def test_ae6_the_second_after_a_clarification_chooses_the_second_candidate_whatever_the_parse_says(world, relation):
    _clarification(world)
    m, _ = _turn(world, "השנייה", [final("בדיקה.", "not_found")], _resolution(
        relation=relation, standalone_question="מה דמי השכירות במתחם הגפן?", intent=intent_part("property", "clarification")))
    req = m["answer"]["request"]
    assert m["answer"]["status"] != "clarification" and req["located_unit"]["display"] == "מתחם הגפן ב"
    assert _decisions(world, m)["pending"].startswith("bound")


def test_a_reply_that_names_no_candidate_is_not_asked_again_even_when_the_model_calls_it_an_answer(world):
    """The office conversation: "כולן" after a clarification led to a second clarification."""
    _clarification(world)
    m, agent = _turn(world, "כולן", [final("בדיקה.", "not_found")], _resolution(
        relation="clarification_answer", standalone_question="כולן", intent=intent_part("property", "clarification")))
    assert m["answer"]["status"] != "clarification" and agent.seen
    assert _decisions(world, m)["pending"].startswith("expired")


def test_after_a_chosen_candidate_a_reply_of_all_of_them_asks_nothing_again(world):
    _clarification(world)
    _turn(world, "השנייה", [final("בדיקה.", "not_found")], _resolution(
        relation="clarification_answer", standalone_question="מה דמי השכירות?", intent=intent_part("property", "clarification")))
    m, agent = _turn(world, "כולן", [final("בדיקה.", "not_found")], _resolution(
        standalone_question="כולן", intent=intent_part("unknown")))
    assert m["answer"]["status"] != "clarification" and agent.seen


def test_a_reply_that_names_two_candidates_reaches_the_model_again_when_the_model_calls_it_an_answer(world):
    _clarification(world)
    again = "הגפן קיים בשני מתחמים. התכוונת לא או לב?"
    m, agent = _turn(world, "הגפן", [final(again, "clarification", clarification=again,
                                           focus=_focus(world.file, "", "unknown", units=[]))],
                     _resolution(relation="clarification_answer", standalone_question="הגפן",
                                 intent=intent_part("property", "clarification")))
    # the server asks nothing: the two candidates the reply names are the model's to weigh (and to ask about, in its words)
    assert m["answer"]["markdown"] == again and len(agent.seen) == 1
    assert not m["answer"]["request"]["candidates"] and m["answer"]["request"]["located_unit"] is None


def test_ae7_a_new_question_holding_a_year_is_not_bound_as_the_pending_parameter(client, office, monkeypatch):
    cid, doc, pending = _clarification_turn(client, office, monkeypatch, [RISE])
    assert pending["parameters"] == ["שיעור העלייה של העלויות"]
    ctx = _stored_context(office, cid)
    assert ctx["pending"]["kind"] == "parameter" and ctx["pending"]["payload"]["parameter"]["parameters"] == pending["parameters"]
    agent = ScriptedAgent([final("בתחשיב אין נתון על 2024.", "not_found", documents=[doc])])
    agent.on("resolve", _resolution(standalone_question="מה היה השווי ב-2024?",
                                    intent=intent_part("property", "conversation_property")))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    m = send(client, cid, "ומה היה השווי ב-2024?")
    assert m["status"] == "done", m
    assert "תשובה לשאלת ההבהרה" not in agent.seen[0][0]["content"]
    assert not m["answer"].get("assumptions") and m["answer"].get("pending") is None
    diag = client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()
    assert diag["resolution"]["decisions"]["pending"].startswith("expired")
    assert _stored_context(office, cid)["pending"] is None


@pytest.mark.parametrize("message, relation, intent, bound", [
    ("8%", "new_question", intent_part("property"), True),  # a number alone is the reply, whatever the parse says
    ("8%", "clarification_answer", intent_part("property", "clarification"), True),
    ("נניח שהעלויות יעלו ב-8 אחוז", "clarification_answer", intent_part("property", "clarification"), True),
    ("נניח שהעלויות יעלו ב-8 אחוז", "new_question", intent_part("property"), False),
    ("מה זה תקן 1?", "clarification_answer", intent_part("professional", "topic"), False),
    ("ומה היה השווי ב-2024?", "new_question", intent_part("property"), False),
])
def test_ae7_a_reply_binds_to_the_pending_parameter_only_when_it_is_the_answer(
        client, office, monkeypatch, message, relation, intent, bound):
    cid, doc, _ = _clarification_turn(client, office, monkeypatch, [RISE])
    agent = ScriptedAgent([final("בדיקה.", "not_found", documents=[doc])])
    agent.on("resolve", _resolution(relation=relation, standalone_question=message, intent=intent))
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    m = send(client, cid, message)
    assert m["status"] == "done", m
    assert ("תשובה לשאלת ההבהרה" in agent.seen[0][0]["content"]) is bound


def test_a_number_alone_is_bound_whatever_its_length_and_whether_or_not_the_resolve_call_succeeded(
        client, office, monkeypatch):
    cid, doc, _ = _clarification_turn(client, office, monkeypatch, [RISE])
    reply = "8.000000000000000000000000%"  # longer than the old cap; still a number alone
    agent = ScriptedAgent([final("בדיקה.", "not_found", documents=[doc])])
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)  # resolve is unscripted: it fails
    m = send(client, cid, reply)
    assert m["status"] == "done", m
    assert any(c.purpose == "resolve" and c.result.status != "ok" for c in agent.calls)
    assert "תשובה לשאלת ההבהרה" in agent.seen[0][0]["content"]


def _stored_context(office, cid: str) -> dict:
    with tenant_tx(office.system) as conn:
        return conn.execute(text(
            "SELECT answer->'context' FROM messages WHERE conversation_id = :c AND role = 'assistant'"
            " ORDER BY created_at DESC LIMIT 1"), {"c": cid}).scalar_one()


# --- reload, permissions and the server-only payload (R10, R15, KTD4) --------------------------------------------------


def test_reload_a_fresh_input_built_from_the_database_yields_the_same_slots(world):
    _rent_turn(world)
    m, _ = _professional_list(world)
    stored = _context(world, m)
    inp = _next_input(world)
    assert inp.focus["document_ids"] == stored["property"]["document_ids"] == [world.rent]
    assert inp.focus["subject"] == stored["property"]["subject"] and inp.focus["metric_kind"] == "rent"
    assert inp.slots["topic"] == stored["topic"] and inp.slots["params"] == stored["params"]
    assert inp.slots["sources"] == stored["sources"]


def test_a_revoked_property_document_drops_the_slot_whole_and_later_professional_answers_stay_visible(world):
    _rent_turn(world)
    _professional_list(world)
    with tenant_tx(world.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": world.rent})
    inp = _next_input(world)
    assert inp.focus is None and inp.slots.get("property") is None and inp.slots.get("params") is None
    assert inp.slot_notes["property"].startswith("dropped")
    again, _ = _turn(world, "ומה הגישה השנייה?", [final("בדיקה.", "not_found")], _resolution(
        standalone_question="ומה הגישה השנייה?", intent=intent_part("professional", "prior_answer_item", item=2)))
    assert _decisions(world, again)["property"].startswith("dropped")
    ctx = _context(world, again)
    assert ctx.get("property") is None and ctx.get("params") is None and _labels(ctx) == APPROACHES
    shown = world.client.get(f"/api/chat/conversations/{world.cid}/messages").json()["messages"]
    answers = [x for x in shown if x["role"] == "assistant"]
    assert answers[0]["answer"].get("hidden") is True  # the property answer itself is gone
    assert answers[1]["answer"]["markdown"] and not answers[1]["answer"].get("hidden")  # the professional one stays
    assert not answers[2]["answer"].get("hidden")
    blob = json.dumps(shown, ensure_ascii=False)
    assert SECRET_SUBJECT not in blob and world.rent not in blob  # neither the subject text nor the id leaks


def test_the_property_slot_is_not_a_document_of_the_answer_that_carries_it(world):
    from app.chat.api import _answer_documents

    _rent_turn(world)
    m, _ = _professional_list(world)
    row = _answer_row(world, m)
    assert row["context"]["property"]["document_ids"] == [world.rent]
    assert world.rent not in _answer_documents(row)


def test_a_hidden_latest_answer_gives_the_turn_no_slot_and_revives_no_older_clarification(world):
    _clarification(world)
    prof, _ = _turn(world, "מה זה שווי שוק?", [
        [call("search", query="שווי שוק", document_ids=[world.guide], limit=None)],
        final("שווי שוק נקבע לפי עסקאות דומות [S1].", claims=_claims("שווי שוק נקבע לפי עסקאות דומות"))],
        _resolution(standalone_question="מה זה שווי שוק?", intent=intent_part("professional", "topic")))
    assert prof["answer"]["status"] == "answered"
    with tenant_tx(world.system) as conn:  # the guide document the latest answer cites is no longer visible
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": world.guide})
    inp = _next_input(world, "השנייה")
    assert inp.focus is None and inp.candidates == [] and inp.pending is None and inp.prior_refs == {}
    assert inp.slots is None and inp.clarified_documents == []
    m, agent = _turn(world, "השנייה", [final("בדיקה.", "not_found")], _resolution(
        relation="clarification_answer", standalone_question="השנייה", intent=intent_part("property", "clarification")))
    assert m["answer"]["status"] != "clarification" and m["answer"]["request"]["document_ids"] == []
    assert "שאלת הבהרה" not in _resolve_input(agent)


def test_the_context_is_server_only_in_every_payload_the_client_receives(world):
    _rent_turn(world)
    m, _ = _professional_list(world)
    assert "context" in _answer_row(world, m)  # stored
    assert "context" not in m["answer"]
    listed = world.client.get(f"/api/chat/conversations/{world.cid}/messages").json()["messages"]
    assert all("context" not in (x["answer"] or {}) for x in listed)
    single = world.client.get(f"/api/chat/messages/{m['id']}").json()
    assert "context" not in single["answer"] and "context" not in json.dumps(single)


# --- messages stored before the slots (legacy) -------------------------------------------------------------------------


def _strip_context(world) -> None:
    with tenant_tx(world.system) as conn:
        conn.execute(text("UPDATE messages SET answer = answer - 'context' WHERE answer IS NOT NULL"))


def test_a_message_without_a_context_still_yields_focus_unit_candidates_and_pending_as_before(world):
    _unit_turn(world)
    _strip_context(world)
    inp = _next_input(world)
    assert inp.focus["document_ids"] == [world.file] and inp.focus["located_unit"]["display"] == "מתחם הדולב"
    assert inp.focus["metric_kind"] == "value" and inp.slots is not None
    third, _ = _turn(world, "ומה השווי שם?", [final("בדיקה.", "not_found")], _resolution(
        relation="same_datum", intent=intent_part("property", "conversation_property")))
    assert third["answer"]["request"]["document_ids"] == [world.file]
    assert "context" in _answer_row(world, third)  # a new message always writes the slots


def test_a_clarification_stored_without_a_context_still_offers_its_candidates(world):
    _clarification(world)
    _strip_context(world)
    inp = _next_input(world)
    assert [c["unit"]["unit_no"] for c in inp.candidates] == [1, 2] and inp.pending is None
    m, _ = _turn(world, "השנייה", [final("בדיקה.", "not_found")], _resolution(
        relation="clarification_answer", standalone_question="מה דמי השכירות?", intent=intent_part("property", "clarification")))
    assert m["answer"]["request"]["located_unit"]["display"] == "מתחם הגפן ב"


def test_a_parameter_clarification_stored_without_a_context_still_binds_the_reply(client, office, monkeypatch):
    cid, doc, pending = _clarification_turn(client, office, monkeypatch, [RISE])
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE messages SET answer = answer - 'context' WHERE answer IS NOT NULL"))
    agent = ScriptedAgent([final("בדיקה.", "not_found", documents=[doc])])
    monkeypatch.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    m = send(client, cid, "8%")
    assert m["status"] == "done", m
    assert "תשובה לשאלת ההבהרה" in agent.seen[0][0]["content"] and pending["parameters"]
