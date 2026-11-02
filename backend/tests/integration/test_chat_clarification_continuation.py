"""A reply binds to the clarification it answers and the original question continues with the choice (round 26 U3; R8-R12; AE3, AE4, AE6,
AE7; KTD3-KTD5).

The model is scripted: these tests prove what the server stores, validates and hands the model -- the pending record, the 409 on a stale
choice, the binding of a click, a number, a name or free text, the continuation of the stored original question with the chosen unit,
period, basis or method, the narrowing, help, cancel and correction replies, and the state the message list and the single-message GET
compute from the conversation's rows. Whether a real model asks and answers well is round 26 U7's measurement. Synthetic documents only."""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import text

from app.chat import clarify
from app.chat.resolve import ResolvedRequest
from app.db import tenant_tx
from app.providers.llm import CallStatus
from tests.conftest import login
from tests.integration import test_chat_clarification as base
from tests.integration.test_chat import cloud, new_conversation
from tests.integration.test_chat_clarification import BROOK_TITLE, HILL_TITLE
from tests.integration.test_chat_context import _resolution
from tests.support.scripted_agent import ScriptedAgent, call, final, intent_part, offer, read
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

NORTH_TITLE = "שומה - הרימון 14 שכונת הנחל הצפוני"
QUESTION = "מה שטח המגרש בהרימון 14?"
ASKED = "יש כמה שומות להרימון 14. לאיזו התכוונת?"


def answer_of(area: int):
    """The answer a continuation gives once it has reopened the chosen appraisal's passage (each appraisal states its own area)."""
    return final(f"שטח המגרש בשומה הוא {area} מ״ר [S1].", claims=[
        {"text": f"שטח המגרש בשומה הוא {area} מ״ר", "source_ids": ["S1"], "basis": "explicit"}])


ANSWER = answer_of(612)  # the Gevaa appraisal
BROOK_ANSWER, NORTH_ANSWER = answer_of(540), answer_of(700)
GENERIC = final("בשומה נמדד שטח המגרש [S1].", claims=[{"text": "בשומה נמדד שטח המגרש", "source_ids": ["S1"], "basis": "explicit"}])
BASES_ANSWER = final("השטח נמדד בשתי דרכים [S1].", claims=[{"text": "השטח נמדד בשתי דרכים", "source_ids": ["S1"], "basis": "explicit"}])
SEARCH = "search"


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "chat_run_inline", True)
    from app.chat import contexts
    from app.platform import pipeline

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    contexts._CACHE.clear()


world = base.world


@pytest.fixture
def cw(world, client, monkeypatch):
    """The clarification world with a conversation, a logged-in client and the cloud on."""
    world.north, _ = unit_file(world, world.default_group_id, NORTH_TITLE, [{"title": None, "blocks": [
        "שומת מקרקעין להרימון 14 בשכונת הנחל הצפוני", "המועד הקובע לשומה: 01/01/2021", "שטח המגרש הוא 700 מ״ר."]}], sha="9" * 64)
    cloud(monkeypatch, world, ScriptedAgent([]))
    login(client, "admin-a@example.test")
    world.client, world.cid, world.mp = client, new_conversation(client), monkeypatch
    return world


# --- helpers ------------------------------------------------------------------------------------------------------------------


def post(cw, content: str, cid: str | None = None, **extra):
    return cw.client.post(f"/api/chat/conversations/{cid or cw.cid}/messages", json={"content": content} | extra)


def messages(cw, cid: str | None = None) -> list[dict]:
    return cw.client.get(f"/api/chat/conversations/{cid or cw.cid}/messages").json()["messages"]


def turn(cw, content: str, steps: list, *, resolve: dict | None = None, intent: str = "property", **extra):
    """One send with a fresh scripted model: the finished assistant message (read with the single-message GET) and the agent."""
    agent = ScriptedAgent(steps, intent=intent)
    if resolve is not None:
        agent.on("resolve", resolve)
    cw.mp.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    r = post(cw, content, **extra)
    assert r.status_code == 200, r.text
    return cw.client.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json(), agent


def docs_of(cw, names=("hill", "brook")) -> list[tuple[str, str]]:
    titles = {"hill": HILL_TITLE, "brook": BROOK_TITLE, "north": NORTH_TITLE}
    return [(titles[n], getattr(cw, n)) for n in names]


def ask(cw, names=("hill", "brook"), *, kind: str = "unit", allow_all: bool = False, extra_steps=(), **extra) -> dict:
    """A first turn that reads the documents and asks which one: the clarifying assistant message."""
    docs = docs_of(cw, names)
    options = docs if kind == "unit" else extra_steps
    m, _ = turn(cw, QUESTION, [[call(SEARCH, query="שטח המגרש", document_ids=[d for _, d in docs_of(cw, names)], limit=None)],
                               final(ASKED, "clarification", clarification=ASKED,
                                     clarify=offer(kind, ASKED, *options, reason="יש כמה שומות באותה כתובת", allow_all=allow_all))],
                **extra)
    assert m["status"] == "done" and m["answer"]["status"] == "clarification", m
    return m


def option_id(m: dict, n: int) -> str:
    return m["answer"]["clarify"]["options"][n]["id"]


def click(cw, m: dict, n, *, content: str = "טקסט שהלקוח שלח", **extra):
    chosen = "all" if n == "all" else option_id(m, n)
    return post(cw, content, clarification={"id": m["answer"]["clarify"]["id"], "option_id": chosen}, **extra)


def clicked(cw, m: dict, n, steps=None, **kw) -> tuple[dict, ScriptedAgent]:
    """A click on option ``n`` with a fresh scripted model (default: read the preloaded source, then answer)."""
    agent = ScriptedAgent(steps if steps is not None else [[read(source="P1")], ANSWER], intent="property")
    cw.mp.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    r = click(cw, m, n, **kw)
    assert r.status_code == 200, r.text
    return cw.client.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json(), agent


def relation_calls(agent: ScriptedAgent) -> list:
    """The calls that read a reply against the conversation (the resolve call); the request analysis of a first turn is another call."""
    return [c for c in agent.calls if c.schema is ResolvedRequest]


def first_message(agent: ScriptedAgent) -> str:
    return agent.seen[0][0]["content"]


def state_of(cw, m: dict) -> dict:
    """The clarification's state on the list path and on the single-message GET: they must agree (KTD5)."""
    listed = next(x for x in messages(cw) if x["id"] == m["id"])["answer"]["clarify"]
    single = cw.client.get(f"/api/chat/messages/{m['id']}").json()["answer"]["clarify"]
    assert (listed["state"], listed.get("chosen")) == (single["state"], single.get("chosen"))
    return {"state": single["state"], "chosen": single.get("chosen")}


def detail(r) -> dict:
    assert r.status_code == 409, (r.status_code, r.text)
    return r.json()["detail"]


# --- the clarifying message carries the clarification and its state (KTD5) -----------------------------------------------------------------


def test_the_clarifying_message_carries_the_clarify_payload_on_the_list_and_on_the_single_message(cw):
    m = ask(cw, allow_all=True)
    clar = m["answer"]["clarify"]
    assert clar["state"] == "open" and clar["chosen"] is None and clar["kind"] == "unit" and clar["allow_all"] is True
    assert clar["question"] == ASKED and clar["reason"] == "יש כמה שומות באותה כתובת"
    assert [o["label"] for o in clar["options"]] == [HILL_TITLE, BROOK_TITLE]
    assert all(set(o) == {"id", "label", "detail"} for o in clar["options"])  # the references never reach the client
    assert m["answer"]["clarification"] == ASKED  # the existing string keeps its meaning
    assert state_of(cw, m) == {"state": "open", "chosen": None}
    assert "context" not in m["answer"] and "pending" in m["answer"]


def test_a_text_clarification_with_no_options_has_no_clarify_payload(cw):
    docs = docs_of(cw)
    m, _ = turn(cw, QUESTION, [[call(SEARCH, query="שטח המגרש", document_ids=[docs[0][1]], limit=None)],
                               final(ASKED, "clarification", clarification=ASKED, clarify=offer("unit", ASKED, *docs))])
    assert m["answer"]["status"] == "clarification" and "clarify" not in m["answer"]


# --- AE3: a click continues the original question with the chosen appraisal -------------------------------------------------------------


def test_ae3_a_click_runs_the_original_question_scoped_to_the_chosen_appraisal_without_a_new_list_or_search(cw):
    m = ask(cw)
    done, agent = clicked(cw, m, 0)
    assert done["status"] == "done" and done["answer"]["status"] == "answered", done
    shown = first_message(agent)
    assert QUESTION in shown and clarify.MSG_CONTINUE_HEAD in shown and HILL_TITLE in shown
    assert "P1" in shown  # the clarifying turn's reads are given back to be reopened
    assert "יחידות מועמדות" not in shown and BROOK_TITLE not in shown  # no candidate list, nothing of the other appraisal
    assert not relation_calls(agent)  # no lookup, no relation call: the choice is bound
    assert len(agent.seen) == 2  # one read of a preloaded source, then the answer: no search
    request = done["answer"]["request"]
    assert request["document_ids"] == [cw.hill] and request["standalone_question"].startswith(QUESTION)
    assert state_of(cw, m) == {"state": "answered", "chosen": option_id(m, 0)}
    assert "clarify" not in done["answer"]


def test_a_click_progress_says_the_original_request_continues_before_the_usual_steps(cw):
    m = ask(cw)
    done, _ = clicked(cw, m, 1)
    labels = [p["label"] for p in done["progress"]]
    assert labels[0] == "ממתין לעיבוד" and "ממשיך את הבקשה המקורית" in labels[:3], labels


def test_a_click_stores_the_user_row_from_the_stored_option_not_from_the_text_the_client_sent_and_hides_the_binding(cw):
    m = ask(cw)
    clicked(cw, m, 1, content="טקסט מוזר שהלקוח המציא")
    user = [x for x in messages(cw) if x["role"] == "user"][-1]
    assert user["content"] == BROOK_TITLE and user["answer"] is None
    with tenant_tx(cw.system) as conn:
        stored = conn.execute(text("SELECT answer FROM messages WHERE id = :m"), {"m": user["id"]}).scalar_one()
    assert stored["clarification_reply"] == {"clarification_id": m["answer"]["clarify"]["id"], "option_id": option_id(m, 1),
                                             "outcome": "answered"}


def test_a_click_does_not_change_the_conversations_time_switch_or_the_time_the_original_question_ran_with(cw):
    m = ask(cw, prefer_recent=False)
    conv = cw.client.get(f"/api/chat/conversations/{cw.cid}/messages").json()["conversation"]
    assert conv["prefer_recent"] is False
    done, _ = clicked(cw, m, 0, prefer_recent=True)
    conv = cw.client.get(f"/api/chat/conversations/{cw.cid}/messages").json()["conversation"]
    assert conv["prefer_recent"] is False
    user = [x for x in messages(cw) if x["role"] == "user"][-1]
    assert user["time_preference"]["prefer_recent"] is False
    diag = cw.client.get(f"/api/chat/messages/{done['id']}/diagnostics").json()
    assert diag["resolution"]["time"]["prefer_recent"] is False and diag["resolution"]["time"]["mode"] == "none"


def test_r12_a_follow_up_that_changes_only_the_metric_keeps_the_chosen_appraisal(cw):
    m = ask(cw)
    clicked(cw, m, 0)
    again, _ = turn(cw, "ומה המועד הקובע?", [final("בדיקה.", "not_found")], resolve=_resolution(
        relation="metric_change", standalone_question="מה המועד הקובע בהרימון 14?", intent=intent_part("property", "conversation_property")))
    assert again["answer"]["request"]["document_ids"] == [cw.hill]


# --- AE7: only the open clarification of this conversation accepts a choice ----------------------------------------------------------------


def test_ae7_a_second_click_with_the_same_client_id_returns_the_same_pair_and_runs_one_turn(cw):
    m = ask(cw)
    agent = ScriptedAgent([[read(source="P1")], ANSWER], intent="property")
    cw.mp.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    cid = str(uuid.uuid4())
    first = click(cw, m, 0, client_id=cid)
    second = click(cw, m, 0, client_id=cid)
    assert first.status_code == second.status_code == 200
    assert first.json()["assistant"]["id"] == second.json()["assistant"]["id"] and len(agent.seen) == 2
    assert sum(1 for x in messages(cw) if x["role"] == "user") == 2


def test_ae7_a_click_on_an_answered_clarification_is_refused_as_no_longer_open(cw):
    m = ask(cw)
    clicked(cw, m, 0)
    body = detail(click(cw, m, 1))
    assert body == {"code": "clarification_stale", "message": "השאלה כבר לא פתוחה"}


def test_ae7_a_click_after_a_newer_answer_is_refused(cw):
    m = ask(cw)
    turn(cw, "מה זה שווי שוק?", [final("בדיקה.", "not_found")], resolve=_resolution(
        standalone_question="מה זה שווי שוק?", intent=intent_part("professional", "topic", topic="שווי שוק")))
    assert detail(click(cw, m, 0))["code"] == "clarification_stale"


def test_a_choice_naming_an_option_the_clarification_does_not_have_is_refused(cw):
    m = ask(cw)
    r = post(cw, "x", clarification={"id": m["answer"]["clarify"]["id"], "option_id": "no-such-option"})
    assert detail(r)["code"] == "clarification_stale"
    r = post(cw, "x", clarification={"id": m["answer"]["clarify"]["id"], "option_id": "all"})  # "all" was not offered
    assert detail(r)["code"] == "clarification_stale"


def test_a_choice_for_another_conversations_clarification_is_refused_and_an_inaccessible_conversation_is_not_found(cw):
    m = ask(cw)
    other = new_conversation(cw.client)
    r = post(cw, "x", cid=other, clarification={"id": m["answer"]["clarify"]["id"], "option_id": option_id(m, 0)})
    assert detail(r)["code"] == "clarification_stale"
    r = post(cw, "x", cid=str(uuid.uuid4()), clarification={"id": m["answer"]["clarify"]["id"], "option_id": option_id(m, 0)})
    assert r.status_code == 404


def test_a_choice_after_a_failed_turn_follows_the_clarification_is_refused_and_the_state_is_stale(cw):
    m = ask(cw)
    failing, _ = turn(cw, "הגבעה", [CallStatus.ERROR])
    assert failing["status"] == "failed"
    assert detail(click(cw, m, 0))["code"] == "clarification_stale"
    assert state_of(cw, m)["state"] == "stale"


def test_a_choice_after_the_user_lost_a_document_of_the_clarification_is_refused(cw):
    m = ask(cw)
    with tenant_tx(cw.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": cw.brook})
    assert detail(click(cw, m, 0))["code"] == "clarification_stale"
    assert "clarify" not in next(x for x in messages(cw) if x["id"] == m["id"])["answer"]  # the hidden message shows nothing


def test_a_choice_of_a_document_lost_between_the_click_and_the_turn_ends_with_no_data(cw):
    m = ask(cw)
    agent = ScriptedAgent([[read(source="P1")], ANSWER], intent="property")
    cw.mp.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    cw.mp.setattr("app.chat.api._start", lambda ctx, cid, aid, uid: None)  # the click is stored; the turn has not started
    r = click(cw, m, 0)
    assert r.status_code == 200
    with tenant_tx(cw.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": cw.hill})
    from app.chat import api

    api.run_message(cw.ctx(), uuid.UUID(cw.cid), uuid.UUID(r.json()["assistant"]["id"]), uuid.UUID(r.json()["user"]["id"]))
    done = cw.client.get(f"/api/chat/messages/{r.json()['assistant']['id']}").json()
    assert done["status"] == "done" and done["answer"]["status"] == "not_found" and agent.seen == []
    assert HILL_TITLE not in json.dumps(done, ensure_ascii=False) and not done["answer"]["sources"]


def test_an_office_that_lost_cloud_sending_refuses_a_choice_with_a_clear_message(cw):
    m = ask(cw)
    with tenant_tx(cw.ctx()) as conn:
        conn.execute(text("UPDATE office_settings SET cloud_llm_enabled = false"))
    assert detail(click(cw, m, 0))["code"] == "clarification_unavailable"


# --- typed replies bind deterministically before any model call (R9; cause 3) --------------------------------------------------------------


@pytest.mark.parametrize("reply, label, answer", [("2", BROOK_TITLE, BROOK_ANSWER), ("השנייה", BROOK_TITLE, BROOK_ANSWER),
                                                  ("שכונת הגבעה", HILL_TITLE, ANSWER), ("הנחל", BROOK_TITLE, BROOK_ANSWER)])
def test_a_number_an_ordinal_or_a_name_binds_the_same_option_without_the_relation_call(cw, reply, label, answer):
    m = ask(cw)
    done, agent = turn(cw, reply, [[read(source="P1")], answer])
    assert done["answer"]["status"] == "answered", done
    assert label in first_message(agent) and QUESTION in first_message(agent)
    assert not relation_calls(agent)  # the resolve call is unscripted and was never needed
    assert state_of(cw, m)["state"] == "answered"
    user = [x for x in messages(cw) if x["role"] == "user"][-1]
    assert user["content"] == reply  # a typed reply keeps its own words


def test_a_bare_number_inside_a_sentence_is_not_taken_as_an_option(cw):
    m = ask(cw)
    done, agent = turn(cw, "מה שווי 2 חדרים?", [final("בדיקה.", "not_found")], resolve=_resolution(
        standalone_question="מה שווי 2 חדרים?", intent=intent_part("property")))
    assert clarify.MSG_CONTINUE_HEAD not in first_message(agent)
    assert state_of(cw, m)["state"] == "closed"


def test_a_reply_that_matches_several_options_narrows_to_those_and_never_shows_the_whole_list_again(cw):
    m = ask(cw, ("hill", "brook", "north"))
    brook, north = cw.brook, cw.north
    again = "ה«הנחל» קיים בשתי שומות. לאיזו התכוונת?"
    narrowed, agent = turn(cw, "הנחל", [final(again, "clarification", clarification=again, clarify=offer(
        "unit", again, (BROOK_TITLE, brook), (NORTH_TITLE, north), reason="שתי שומות בשכונת הנחל"))])
    shown = first_message(agent)
    assert narrowed["answer"]["status"] == "clarification" and HILL_TITLE not in shown and cw.hill not in shown
    assert BROOK_TITLE in shown and NORTH_TITLE in shown
    assert [o["label"] for o in narrowed["answer"]["clarify"]["options"]] == [BROOK_TITLE, NORTH_TITLE]
    assert narrowed["answer"]["clarify"]["id"] != m["answer"]["clarify"]["id"]
    assert state_of(cw, m)["state"] == "closed" and state_of(cw, narrowed)["state"] == "open"
    # the narrowed question still continues the first one, and a name settles it
    done, agent2 = turn(cw, "הצפוני", [[read(source="P1")], NORTH_ANSWER])
    assert done["answer"]["status"] == "answered" and NORTH_TITLE in first_message(agent2) and QUESTION in first_message(agent2)


def test_a_reply_that_names_none_of_the_options_is_new_information_for_the_original_question(cw):
    m = ask(cw)
    done, agent = turn(cw, "ההרימון שליד הים", [[read(source="P1")], GENERIC], resolve=_resolution(
        relation="clarification_answer", standalone_question="ההרימון שליד הים", intent=intent_part("property", "clarification")))
    shown = first_message(agent)
    assert clarify.MSG_CONTINUE_HEAD in shown and "ההרימון שליד הים" in shown and QUESTION in shown
    assert state_of(cw, m)["state"] == "answered"
    assert done["answer"]["status"] == "answered"


def test_ae4_an_unrelated_question_is_answered_the_clarification_closes_and_its_buttons_do_nothing(cw):
    m = ask(cw)
    done, agent = turn(cw, "מה זה שווי שוק?", [final("שווי שוק נקבע לפי עסקאות דומות.", "not_found")], resolve=_resolution(
        standalone_question="מה זה שווי שוק?", intent=intent_part("professional", "topic", topic="שווי שוק")))
    assert done["answer"]["status"] == "not_found" and clarify.MSG_CONTINUE_HEAD not in first_message(agent)
    assert state_of(cw, m)["state"] == "closed" and "clarify" not in done["answer"]
    assert detail(click(cw, m, 0))["code"] == "clarification_stale"


def test_a_failed_relation_call_with_a_reply_that_is_not_an_option_leaves_the_clarification_closed_and_asks_nothing_again(cw):
    m = ask(cw)
    done, agent = turn(cw, "מה דעתך על זה?", [final("בדיקה.", "not_found")])  # the resolve call is unscripted: it fails
    assert clarify.MSG_CONTINUE_HEAD not in first_message(agent) and state_of(cw, m)["state"] in ("closed", "stale")


# --- cancel, help, correction ----------------------------------------------------------------------------------------------------------------


def test_cancel_closes_the_clarification_with_a_short_acknowledgement_and_no_model_call(cw):
    m = ask(cw)
    done, agent = turn(cw, "ביטול", [])
    assert agent.seen == [] and done["answer"]["status"] == "answered" and done["content"] == clarify.MSG_CANCELLED
    assert state_of(cw, m)["state"] == "closed" and "clarify" not in done["answer"]
    assert detail(click(cw, m, 0))["code"] == "clarification_stale"


def test_help_answers_with_both_readings_and_keeps_the_options_open_once_under_a_new_id(cw):
    m = ask(cw)
    done, agent = turn(cw, "לא יודע", [final("בשכונת הגבעה המועד הקובע 2024 ובשכונת הנחל 2022.", "answered")])
    assert clarify.MSG_HELP in first_message(agent) and QUESTION in first_message(agent)
    again = done["answer"]["clarify"]
    assert again["state"] == "open" and again["id"] != m["answer"]["clarify"]["id"]
    assert [o["id"] for o in again["options"]] == [o["id"] for o in m["answer"]["clarify"]["options"]]
    assert state_of(cw, m)["state"] == "closed"
    assert detail(click(cw, m, 0))["code"] == "clarification_stale"  # the old id is stale
    chosen, agent2 = clicked(cw, done, 1, steps=[[read(source="P1")], BROOK_ANSWER])  # the new buttons work
    assert BROOK_TITLE in first_message(agent2) and chosen["answer"]["status"] == "answered"


def test_a_second_help_does_not_keep_the_clarification_open_the_turn_answers_with_stated_assumptions(cw):
    m = ask(cw)
    first, _ = turn(cw, "לא יודע", [final("הסבר.", "answered")])
    second, agent = turn(cw, "לא יודע", [final("עניתי עם הנחה מוצהרת.", "answered")])
    assert clarify.MSG_ASSUMPTIONS in first_message(agent) and "clarify" not in second["answer"]
    assert state_of(cw, first)["state"] == "closed" and state_of(cw, m)["state"] == "closed"


def test_a_correction_in_the_next_message_reanswers_the_original_question_for_the_corrected_appraisal(cw):
    m = ask(cw)
    clicked(cw, m, 0)
    brook_id = option_id(m, 1)
    done, agent = turn(cw, "לא, התכוונתי לשכונת הנחל", [[read(source="P1")], BROOK_ANSWER], resolve=_resolution(
        relation="correction_of_choice", standalone_question="לא, התכוונתי לשכונת הנחל", chosen_options=[brook_id],
        intent=intent_part("property", "clarification")))
    shown = first_message(agent)
    assert QUESTION in shown and BROOK_TITLE in shown and clarify.MSG_CONTINUE_HEAD in shown
    assert done["answer"]["request"]["document_ids"] == [cw.brook]


def test_a_correction_two_messages_later_is_the_ordinary_correction_not_a_rebinding(cw):
    m = ask(cw)
    clicked(cw, m, 0)
    turn(cw, "ומה המועד הקובע?", [final("בדיקה.", "not_found")], resolve=_resolution(
        relation="metric_change", standalone_question="מה המועד הקובע?", intent=intent_part("property", "conversation_property")))
    done, agent = turn(cw, "לא, התכוונתי לשכונת הנחל", [final("בדיקה.", "not_found")], resolve=_resolution(
        relation="correction_of_choice", standalone_question="לא, התכוונתי לשכונת הנחל", chosen_options=[option_id(m, 1)],
        intent=intent_part("property", "clarification")))
    assert clarify.MSG_CONTINUE_HEAD not in first_message(agent)


# --- AE6: "all" answers per appraisal ----------------------------------------------------------------------------------------------------------


def test_ae6_all_asks_the_model_to_answer_each_appraisal_apart_with_no_joint_average(cw):
    m = ask(cw, allow_all=True)
    both = final("בשומה בשכונת הגבעה 612 מ״ר [S1], ובשומה בשכונת הנחל 540 מ״ר [S1]. אין ממוצע משותף.", claims=[
        {"text": "בשומה בשכונת הגבעה 612 מ״ר", "source_ids": ["S1"], "basis": "explicit"}])
    done, agent = clicked(cw, m, "all", steps=[[read(source="P1")], both])
    shown = first_message(agent)
    assert HILL_TITLE in shown and BROOK_TITLE in shown and clarify.MSG_ALL_RULE in shown
    assert state_of(cw, m) == {"state": "answered", "chosen": "all"}
    user = [x for x in messages(cw) if x["role"] == "user"][-1]
    assert user["content"] == clarify.MSG_ALL


# --- period, area basis and method ------------------------------------------------------------------------------------------------------------


PERIODS = [("מרץ 2024", "2024-03-15"), ("ספטמבר 2022", "2022-09-02")]


def test_a_period_choice_runs_the_original_question_with_an_explicit_period_over_the_switch(cw):
    m = ask(cw, kind="period", extra_steps=PERIODS, prefer_recent=False)
    done, agent = clicked(cw, m, 0, prefer_recent=True)
    diag = cw.client.get(f"/api/chat/messages/{done['id']}/diagnostics").json()
    t = diag["resolution"]["time"]
    assert t["mode"] == "explicit" and t["source"] == "clarification" and t["requested"]["start"] == "2024-03-15"
    assert clarify.MSG_CONTINUE_HEAD in first_message(agent) and QUESTION in first_message(agent)
    assert cw.client.get(f"/api/chat/conversations/{cw.cid}/messages").json()["conversation"]["prefer_recent"] is False


@pytest.mark.parametrize("kind", ["area_basis", "method"])
def test_an_area_basis_or_method_choice_is_labelled_the_users_assumption(cw, kind):
    asked = "באיזה בסיס שטח?"
    steps = [[call(SEARCH, query="שטח ברוטו נטו", document_ids=[cw.bases], limit=None)],
             final(asked, "clarification", clarification=asked, clarify=offer(kind, asked, ("שטח ברוטו", "שטח ברוטו"), ("שטח נטו", "שטח נטו")))]
    m, _ = turn(cw, "מה שטח הנכס?", steps)
    assert m["answer"]["clarify"]["kind"] == kind
    done, agent = clicked(cw, m, 1, steps=[[read(source="P1")], BASES_ANSWER])
    shown = first_message(agent)
    assert "שטח נטו" in shown and clarify.MSG_USER_ASSUMPTION in shown and "מה שטח הנכס?" in shown
    assert done["answer"]["status"] == "answered"


# --- retry keeps the choice (G1) -----------------------------------------------------------------------------------------------------------------


def test_retrying_a_failed_continuation_keeps_the_chosen_option_and_the_original_time(cw):
    m = ask(cw, prefer_recent=False)
    failed, _ = clicked(cw, m, 1, steps=[CallStatus.ERROR])
    assert failed["status"] == "failed" and state_of(cw, m) == {"state": "answered", "chosen": option_id(m, 1)}
    agent = ScriptedAgent([[read(source="P1")], BROOK_ANSWER], intent="property")
    cw.mp.setattr("app.providers.llm.get_selected_provider", lambda: agent)
    r = cw.client.post(f"/api/chat/messages/{failed['id']}/retry")
    assert r.status_code == 200, r.text
    again = cw.client.get(f"/api/chat/messages/{r.json()['id']}").json()
    assert again["status"] == "done" and BROOK_TITLE in first_message(agent) and QUESTION in first_message(agent)
    assert not relation_calls(agent)
    diag = cw.client.get(f"/api/chat/messages/{again['id']}/diagnostics").json()
    assert diag["resolution"]["time"]["prefer_recent"] is False  # the time the original question ran under, not the conversation's


# --- a pending row written before version 3 still binds as it did (reader) -------------------------------------------------------------------------


def test_a_version_two_pending_row_still_binds_by_ordinal_as_before(cw):
    m = ask(cw)
    with tenant_tx(cw.system) as conn:
        row = conn.execute(text("SELECT answer FROM messages WHERE id = :m"), {"m": m["id"]}).scalar_one()
        ctx = row["context"]
        payload = ctx["pending"]["payload"]
        ctx["version"] = 2
        ctx["pending"] = {"kind": "candidates", "asked_at": ctx["pending"]["asked_at"],
                          "payload": {"candidates": payload["candidates"], "named_document_ids": [], "clarified": []}}
        row["context"] = ctx
        row.pop("clarify", None)
        conn.execute(text("UPDATE messages SET answer = CAST(:a AS jsonb) WHERE id = :m"), {"a": json.dumps(row), "m": m["id"]})
    done, agent = turn(cw, "השנייה", [final("בדיקה.", "not_found")], resolve=_resolution(
        relation="clarification_answer", standalone_question="מה שטח המגרש?", intent=intent_part("property", "clarification")))
    assert done["answer"]["request"]["document_ids"] == [cw.brook]


# --- a unit of a file of several appraisals (untitled units told apart by address), and the cap of two (R10) ----------------------------------


def test_a_click_on_an_untitled_unit_runs_the_original_question_inside_that_unit(cw):
    asked = "בקובץ שני נכסים ברחוב הצאלון. לאיזה התכוונת?"

    def step(items):
        return final(asked, "clarification", clarification=asked, clarify=offer(
            "unit", asked, ("אחד", base.handle_of(items, "הצאלון 8")), ("שני", base.handle_of(items, "הצאלון 12"))))

    m, _ = turn(cw, "מה שטח המגרש ברחוב הצאלון?", [step])
    assert [o["label"] for o in m["answer"]["clarify"]["options"]] == ["רחוב הצאלון 8", "רחוב הצאלון 12"]
    done, agent = clicked(cw, m, 1, steps=[final("בדיקה.", "not_found")])
    shown = first_message(agent)
    request = done["answer"]["request"]
    assert request["located_unit"]["unit_no"] == 3 and request["document_ids"] == [cw.bound] and request["unit_kept"] is False
    assert "הצאלון 12" in shown and "מה שטח המגרש ברחוב הצאלון?" in shown and "הצאלון 8" not in shown.split(clarify.MSG_CONTINUE_HEAD)[1]
    assert done["answer"]["unit"]["unit_no"] == 3  # the conversation is in the chosen unit now
    # and a follow-up that changes only the metric stays in it
    again, _ = turn(cw, "ומה המועד הקובע?", [final("בדיקה.", "not_found")], resolve=_resolution(
        relation="metric_change", standalone_question="מה המועד הקובע?", intent=intent_part("property", "conversation_property")))
    assert again["answer"]["request"]["located_unit"]["unit_no"] == 3


def test_r10_a_free_text_answer_counts_as_a_clarification_and_a_third_in_a_row_is_answered_with_assumptions(cw):
    m = ask(cw)
    again = "עדיין לא ברור: לאיזו שומה?"
    reopen = final(again, "clarification", clarification=again, clarify=offer("unit", again, (HILL_TITLE, cw.hill), (BROOK_TITLE, cw.brook)))
    both = [read(source="P1"), read(source="P3")]  # the reopened reads ground the options of a further question
    second, _ = turn(cw, "ההרימון שליד הים", [both, reopen], resolve=_resolution(
        relation="clarification_answer", standalone_question="ההרימון שליד הים", intent=intent_part("property", "clarification")))
    assert second["answer"]["status"] == "clarification" and len(second["answer"]["clarify"]["options"]) == 2
    assert state_of(cw, m)["state"] == "answered"
    third, agent = turn(cw, "בהרימון הרגיל", [both, reopen, GENERIC], resolve=_resolution(
        relation="clarification_answer", standalone_question="בהרימון הרגיל", intent=intent_part("property", "clarification")))
    assert third["answer"]["status"] != "clarification" and "clarify" not in third["answer"]
    notices = [i["content"] for step in agent.seen for i in step if isinstance(i, dict) and i.get("role") == "user"]
    assert clarify.MSG_CAP in notices[-1]  # the model asked a third time: one re-prompt told it to answer with assumptions
