"""Reproductions of the first two causes of the repeated candidate list (round 26 U2; plan 2026-11-02-1055, Problem Frame).

A user who picks a property after a clarification used to get the same candidate list again. Cause 1: the model's
clarification stored no candidates, so nothing bound the reply. Cause 2: the units were untitled, so the list read "ללא
כותרת" for each of them and no name told them apart. Each test below states what the user needs and failed on the
code it was written against (HEAD 6c75986, before the structured clarification). Causes 3-5 (the reply's binding, the
re-offer after a pick, the stale pending) belong to U3 and are proved in its own tests. Synthetic documents only."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.chat import entities
from app.db import tenant_tx
from tests.integration import test_chat_clarification as base
from tests.integration.test_chat_clarification import (
    BOUND_TITLE,
    BROOK_TITLE,
    HILL_TITLE,
    LIST,
    NO_TITLE,
    handle_of,
    run,
    two_documents,
)
from tests.support.scripted_agent import final, offer

pytestmark = pytest.mark.db

world = base.world  # the fixtures of the clarification tests, as they are


# --- cause 1: the clarification stored no candidates ---------------------------------------------------------------------


def test_cause1_a_clarification_between_two_documents_stores_both_as_the_candidates_a_reply_binds_to(world):
    """The model asked between two documents of one address. With no ``focus.units`` (documents are no units) nothing was
    offered, so the pending slot was empty and the reply "the Gevaa one" went to the model as a new question."""
    out, _ = two_documents(world)
    assert [c["document_id"] for c in out.offered_units] == [world.hill, world.brook]
    assert [c["title"] for c in out.offered_units] == [HILL_TITLE, BROOK_TITLE]


def test_cause1_the_candidates_come_from_the_options_even_when_the_model_declares_no_focus_units(world):
    asked = "בקובץ שני נכסים ברחוב הצאלון. לאיזה התכוונת?"

    def step(items):  # ``final`` sends ``focus: None``: no ``focus.units`` at all
        return final(asked, "clarification", clarification=asked, clarify=offer(
            "unit", asked, ("אחד", handle_of(items, "הצאלון 8")), ("שני", handle_of(items, "הצאלון 12"))))

    out, _ = run(world, "מה שטח המגרש ברחוב הצאלון?", [step])
    assert out.answer.focus is None
    assert [c["unit"]["unit_no"] for c in out.offered_units] == [2, 3] and len(out.clarification["options"]) == 2


def test_cause1_a_text_question_that_offered_nothing_still_leaves_a_record_with_the_documents_it_touched(world):
    """Fewer than two valid options: no buttons, but a pending record exists and names the documents behind the
    question, so a typed unique name can still bind (U3 stores it)."""
    asked = "יש שתי שומות להרימון 14 - בשכונת הגבעה ובשכונת הנחל. לאיזו התכוונת?"
    out, _ = run(world, "מה שטח המגרש בהרימון 14?", [LIST, final(asked, "clarification", clarification=asked)])
    assert out.clarification["options"] == [] and out.clarification["question"] == asked
    assert {world.hill, world.brook} <= set(out.shown_documents)


def test_cause1_an_invented_handle_does_not_empty_the_candidates_of_the_valid_options(world):
    asked = "לאיזו שומה התכוונת?"
    out, _ = run(world, "מה שטח המגרש בהרימון 14?", [LIST, final(asked, "clarification", clarification=asked, clarify=offer(
        "unit", asked, (HILL_TITLE, world.hill), (BROOK_TITLE, world.brook), ("שומה אחרת", "U9")))])
    assert [c["document_id"] for c in out.offered_units] == [world.hill, world.brook]
    assert out.clarification_check["dropped"] == [{"key": "3", "reason": "unit_not_offered"}]


# --- cause 2: untitled units read "ללא כותרת" and cannot be told apart --------------------------------------------------


def _untitled_candidates(world) -> list[dict]:
    places = [(world.bound, None, 2), (world.bound, None, 3)]
    with tenant_tx(world.system) as conn:
        reading = conn.execute(text("SELECT ingestion->>'reading_id' FROM document_versions WHERE id = :v"),
                               {"v": world.bound_ver}).scalar_one()
    return entities.offered_candidates(world.ctx(), [(d, reading, n) for d, _, n in places])


def test_cause2_two_untitled_units_are_named_by_address_and_date_in_the_label_of_a_clarification(world):
    candidates = _untitled_candidates(world)
    labels = [entities.candidate_label(c) for c in candidates]
    assert len(labels) == 2 and labels[0] != labels[1]
    assert all(NO_TITLE not in label for label in labels), labels
    assert "הצאלון 8" in labels[0] and "הצאלון 12" in labels[1]
    assert "01/02/2023" in labels[0] and "20/06/2024" in labels[1] and BOUND_TITLE in labels[0]


def test_cause2_the_candidates_carry_the_details_that_tell_the_units_apart(world):
    first, second = _untitled_candidates(world)
    assert first["details"]["address"] == "רחוב הצאלון 8" and second["details"]["address"] == "רחוב הצאלון 12"
    assert first["details"]["date"] == "01/02/2023" and second["details"]["date"] == "20/06/2024"
    assert first["details"]["pages"] == "עמוד 2" and second["details"]["pages"] == "עמוד 3"
    assert first["details"]["file_title"] == BOUND_TITLE


# --- causes 3, 4 and 5 (round 26 U3): the reply's binding, the re-offer after a pick, the stale pending ---------------------------
# They run through the API, because the binding lives in the conversation's rows. Each failed on bed9e93 (U2 only).

from app.providers.llm import CallStatus  # noqa: E402
from tests.integration import test_chat_clarification_continuation as cont  # noqa: E402
from tests.integration.test_chat_context import _resolution  # noqa: E402
from tests.support.scripted_agent import intent_part, read  # noqa: E402

cw = cont.cw  # the conversation world of the continuation tests
no_models = cont.no_models  # the models off, and the turns run inline


def test_cause3_a_failed_relation_call_still_binds_a_reply_that_names_one_option(cw):
    """The reply "שכונת הגבעה" is no ordinal: it bound only when the small relation call said it answered the clarification, and a
    failed call bound nothing -- the reply became a new question and the candidates were listed again."""
    cont.ask(cw)
    done, agent = cont.turn(cw, "שכונת הגבעה", [[read(source="P1")], cont.ANSWER])  # the resolve call is unscripted: it fails
    shown = cont.first_message(agent)
    assert done["answer"]["request"] is not None and done["answer"]["request"]["document_ids"] == [cw.hill]
    assert cont.QUESTION in shown and "יחידות מועמדות" not in shown and cont.BROOK_TITLE not in shown


def test_cause4_a_reply_that_matches_several_options_narrows_to_them_and_does_not_list_all_of_them_again(cw):
    """"הנחל" names two of the three appraisals. It re-offered the same full list, the appraisal the user did not mean included."""
    cont.ask(cw, ("hill", "brook", "north"))
    again = "יש שתי שומות בשכונת הנחל. לאיזו התכוונת?"
    done, agent = cont.turn(cw, "הנחל", [final(again, "clarification", clarification=again, clarify=offer(
        "unit", again, (cont.BROOK_TITLE, cw.brook), (cont.NORTH_TITLE, cw.north)))], resolve=_resolution(
        relation="clarification_answer", standalone_question="הנחל", intent=intent_part("property", "clarification")))
    shown = cont.first_message(agent)
    assert cont.HILL_TITLE not in shown and cw.hill not in shown
    assert [o["label"] for o in done["answer"]["clarify"]["options"]] == [cont.BROOK_TITLE, cont.NORTH_TITLE]


def test_cause5_a_failed_turn_after_the_clarification_makes_it_stale_so_an_older_one_is_not_resurrected(cw):
    """Clarification, then a reply whose turn failed, then the reply again: the failed message sat between, and the latest DONE assistant
    message was still the clarification -- so "השנייה" bound its candidates a second time."""
    cont.ask(cw)
    failed, _ = cont.turn(cw, "מה זה שווי שוק?", [CallStatus.ERROR])
    assert failed["status"] == "failed"
    done, agent = cont.turn(cw, "השנייה", [final("בדיקה.", "not_found")], resolve=_resolution(
        relation="clarification_answer", standalone_question="השנייה", intent=intent_part("property", "clarification")))
    assert not (done["answer"].get("request") or {}).get("document_ids")
