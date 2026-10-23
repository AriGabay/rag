"""Round 22 U12 sequences: conversation state v2 (KTD14; R20, R21, R22, R23; AE6; N3, N4, N15, N19). Synthetic office, scripted model and judge.

The lists an answer prints are built when it is composed and stored in the context; the next turn binds "השני", "התשלום השני", "ה-12" and
"שניהם" to the right list, or asks one question that names the lists. A follow-up changes only what the user changed: a metric change and a
standards question keep the property, and an explicit period of the previous question carries to a follow-up that names no time. Each
carried list is gated by its own documents. Everything is invented; the models are scripted, so these tests prove what the server decides.

1. AE6: the second payment binds to the payments list of an answer that printed plots and payments.
2. A bare ordinal over two lists asks one question that names both; the lists survive the question.
3. "ה-12" binds in a list of 15; a year, a block number and a percentage do not.
4. "שניהם" binds a two-item list and asks over three.
5. A metric change keeps the property; a standards question keeps it too, and its answer cites no property source.
6. An explicit period carries to a follow-up that names no time, and expires on a property change; a new time replaces it; with the
   "latest" switch on an explicit period still wins.
7. Hiding one list's document drops that list only; the other list keeps its id.
8. An answer stored before it had lists (version 1 context, or none) resolves through the legacy reader.
"""

# ruff: noqa: F811 - the tests take the imported fixtures by name

from __future__ import annotations

import pytest
from sqlalchemy import text

from app.db import tenant_tx
from tests.integration.test_chat_context import (
    SECRET_SUBJECT,
    _claims,
    _context,
    _decisions,
    _focus,
    _next_input,
    _resolution,
    _resolve_input,
    _task,
    _turn,
    no_models,  # noqa: F401 - the autouse inline-run setup
    world,  # noqa: F401 - the synthetic office
)
from tests.support.scripted_agent import call, final, intent_part

pytestmark = pytest.mark.db

PLOTS = ["הדקל", "התמר", "האלון", "הארז", "הברוש", "הגפן", "הדולב", "הזית", "החרוב", "הלוז", "המילה", "הנרקיס", "הסביון", "העפרוני", "הפרג"]
PLOTS_AND_PAYMENTS = ("להלן המגרשים בשומה:\n1. **מגרש הדקל** — שטח גדול [S1]\n2. **מגרש התמר** — שטח בינוני [S1]\n"
                      "3. **מגרש האלון** — שטח קטן [S1]\n\n**לוח התשלומים:**\n- תשלום ראשון — בחתימה [S1]\n"
                      "- תשלום שני — במסירה [S1]\n- תשלום שלישי — באכלוס [S1]")
SUBJECT = "התמר 9"


def property_turn(world, question: str, markdown: str, *, resolve: dict | None = None, doc: str | None = None, subject: str = SUBJECT,
                  kind: str = "rent", intent: str = "property", **kw):
    """A turn that answers about the property of ``doc`` (the rent document), with ``markdown`` as its text."""
    doc = doc or world.rent
    return _turn(world, question, [
        [call("search", query=question, document_ids=[doc], limit=None)],
        final(markdown, focus=_focus(doc, subject, kind), claims=_claims(markdown.split("[S1]")[0].strip().splitlines()[-1][:60]))],
        resolve, intent=intent, **kw)


def printed(world, markdown: str = PLOTS_AND_PAYMENTS) -> dict:
    m, _ = property_turn(world, "אילו מגרשים ותשלומים יש בשומה?", markdown)
    return m


def follow_up(world, question: str, claim: dict, *, answer: str = "ההסבר לפי הנכס [S1].", **resolve_kw):
    """A follow-up whose resolve call says ``claim`` (an ``intent_part``) and whose agent answers about the same property."""
    return _turn(world, question, [
        [call("search", query=question, document_ids=[world.rent], limit=None)],
        final(answer, focus=_focus(world.rent, SUBJECT), claims=_claims("ההסבר לפי הנכס"))],
        _resolution(relation="same_datum", standalone_question=question, intent=claim, **resolve_kw))


# --- 1. AE6 -------------------------------------------------------------------------------------------------------------------------


def test_ae6_the_second_payment_binds_to_the_payments_list_when_the_answer_printed_plots_and_payments(world):
    m1 = printed(world)
    plots, payments = _context(world, m1)["lists"]
    assert (plots["title"], plots["kind"], plots["total"]) == ("להלן המגרשים בשומה", "מגרש", 3)
    assert (payments["title"], payments["kind"], payments["total"]) == ("לוח התשלומים", "תשלום", 3)
    assert [i["label"] for i in payments["items"]] == ["תשלום ראשון", "תשלום שני", "תשלום שלישי"]
    assert plots["document_ids"] == payments["document_ids"] == [world.rent] and payments["id"] == f"{m1['id']}#2"

    m2, agent = follow_up(world, "ומה לגבי התשלום השני?", intent_part("property", "prior_answer_item", item=2, list_id=payments["id"]))
    req = m2["answer"]["request"]
    assert req["intent"]["item"] == 2 and req["intent"]["item_label"] == "תשלום שני" and req["intent"]["list_id"] == payments["id"]
    assert req["topic"] == "תשלום שני" and "תשלום שני" in req["standalone_question"]
    assert _decisions(world, m2)["item"].startswith("bound: 2 «תשלום שני»")
    assert "מגרש התמר" not in req["standalone_question"]  # not plot two
    task = _task(agent)
    assert "תשלום שני" in task and "לוח התשלומים" in task
    seen = _resolve_input(agent)  # the resolve call was shown both lists, each with its id and title
    assert f"רשימה {plots['id']} «להלן המגרשים בשומה»" in seen and f"רשימה {payments['id']} «לוח התשלומים»" in seen
    assert "2. תשלום שני" in seen and "2. מגרש התמר" in seen


def test_the_model_naming_the_wrong_list_does_not_move_the_binding_off_the_users_noun(world):
    m1 = printed(world)
    plots, _ = _context(world, m1)["lists"]
    m2, _ = follow_up(world, "ומה לגבי התשלום השני?", intent_part("property", "prior_answer_item", item=2, list_id=plots["id"]))
    assert m2["answer"]["request"]["intent"]["item_label"] == "תשלום שני"  # the server's choice, validated item


# --- 2. a bare ordinal over two lists -----------------------------------------------------------------------------------------------


def test_a_bare_ordinal_over_two_plausible_lists_asks_one_question_naming_both_and_the_lists_survive_it(world):
    m1 = printed(world)
    _, payments = _context(world, m1)["lists"]
    m2, agent = _turn(world, "ומה לגבי השני?", [], _resolution(
        relation="same_datum", standalone_question="ומה לגבי השני?", intent=intent_part("property", "prior_answer_item", item=2)))
    a = m2["answer"]
    assert a["status"] == "clarification" and agent.seen == []  # the server asked; no model answer was taken
    assert "להלן המגרשים בשומה" in a["markdown"] and "לוח התשלומים" in a["markdown"] and a["markdown"].count("?") == 1
    assert _decisions(world, m2)["item"].startswith("ask")
    ctx = _context(world, m2)
    assert [x["id"] for x in ctx["lists"]] == [x["id"] for x in _context(world, m1)["lists"]] and ctx["lists"][0]["age"] == 1
    # the user's reply names the list: the same lists bind it
    m3, _ = follow_up(world, "התשלום השני", intent_part("property", "prior_answer_item", item=2, list_id=payments["id"]))
    assert m3["answer"]["request"]["intent"]["item_label"] == "תשלום שני"


# --- 3. "ה-12" and the numbers that are not places -----------------------------------------------------------------------------------


def test_the_twelfth_binds_in_a_list_of_fifteen_and_is_ignored_for_a_year_and_a_block_number(world):
    fifteen = "מגרשי השומה:\n" + "\n".join(f"{i}. **מגרש {name}** — שטח [S1]" for i, name in enumerate(PLOTS, 1))
    m1 = printed(world, fifteen)
    (entry,) = _context(world, m1)["lists"]
    assert entry["total"] == 15 and len(entry["items"]) == 15 and entry["kind"] == "מגרש"
    m2, _ = follow_up(world, "ומה לגבי המגרש ה-12?", intent_part("property", "prior_answer_item", item=12, list_id=entry["id"]))
    assert m2["answer"]["request"]["intent"]["item_label"] == "מגרש הנרקיס"
    # (the lists live for three answers, so two more turns still hold them; the percentage is proven in the unit tests)
    for message in ("ומה היה השווי ב-2012?", "ומה השטח בגוש 6012?"):
        m, _ = follow_up(world, message, intent_part("property", "prior_answer_item", item=12, list_id=entry["id"]))
        assert m["answer"]["request"]["intent"]["item"] is None, message
        assert _decisions(world, m)["item"].startswith("rejected: no ordinal"), message


# --- 4. "שניהם" ---------------------------------------------------------------------------------------------------------------------


def test_both_binds_a_list_of_two_items_and_asks_over_three(world):
    two = "הבניינים בשומה:\n1. **בניין הדקל** — שטח [S1]\n2. **בניין התמר** — שטח [S1]"
    m1 = printed(world, two)
    (entry,) = _context(world, m1)["lists"]
    m2, agent = follow_up(world, "ומה השווי של שניהם?", intent_part("property", "prior_answer_item", list_id=entry["id"], item_set=[1, 2],
                                                                   item_scope="both"))
    req = m2["answer"]["request"]
    assert req["intent"]["item_set"] == [1, 2] and req["intent"]["item_labels"] == ["בניין הדקל", "בניין התמר"]
    assert "בניין הדקל" in req["standalone_question"] and "בניין התמר" in req["standalone_question"]
    assert "שני הפריטים ברשימה" in _task(agent)


def test_both_over_three_items_asks_and_never_binds_all(world):
    three = "הבניינים בשומה:\n1. **בניין הדקל** — שטח [S1]\n2. **בניין התמר** — שטח [S1]\n3. **בניין האלון** — שטח [S1]"
    m1 = printed(world, three)
    (entry,) = _context(world, m1)["lists"]
    m2, agent = _turn(world, "ומה השווי של שניהם?", [], _resolution(
        relation="same_datum", standalone_question="ומה השווי של שניהם?",
        intent=intent_part("property", "prior_answer_item", list_id=entry["id"], item_set=[1, 2], item_scope="both")))
    assert m2["answer"]["status"] == "clarification" and "3 פריטים" in m2["answer"]["markdown"] and agent.seen == []
    assert m2["answer"]["request"]["intent"]["item_set"] == []


# --- 5. a metric change and a standards question keep the property ------------------------------------------------------------------


def test_a_metric_change_keeps_the_property_and_changes_only_the_metric_parameters(world):
    m1, _ = property_turn(world, "מה דמי השכירות הראויים?", "דמי השכירות הראויים נקבעו לפי הנכס [S1].", subject=SECRET_SUBJECT)
    before = _context(world, m1)
    m2, _ = _turn(world, "ומה לגבי השווי?", [
        [call("search", query="שווי", document_ids=[world.rent], limit=None)],
        final("השווי נקבע לפי הנכס [S1].", focus=_focus(world.rent, SECRET_SUBJECT, "value"), claims=_claims("השווי נקבע לפי הנכס"))],
        _resolution(relation="metric_change", standalone_question="מה השווי של הנכס?", metric_kind="value",
                    changed_fields=[{"field": "metric_kind", "user_words": "השווי"}],
                    intent=intent_part("property", "conversation_property")))
    after = _context(world, m2)
    assert m2["answer"]["request"]["document_ids"] == [world.rent]
    assert after["property"] == before["property"]  # the same property, subject and unit
    assert before["params"]["metric_kind"] == "rent" and after["params"]["metric_kind"] == "value"
    # a metric change whose answer declares no focus leaves the property where it was
    m3, _ = _turn(world, "ומה לגבי התשואה?", [final("לא נמצא נתון.", "not_found")], _resolution(
        relation="metric_change", standalone_question="מה התשואה של הנכס?", intent=intent_part("property", "conversation_property")))
    assert _context(world, m3)["property"] == before["property"]


def test_a_standards_question_keeps_the_property_the_next_property_follow_up_uses_it_and_the_standards_answer_cites_no_property(world):
    m1, _ = property_turn(world, "מה דמי השכירות הראויים?", "דמי השכירות הראויים נקבעו לפי הנכס [S1].", subject=SECRET_SUBJECT)
    first = _context(world, m1)
    m2, _ = _turn(world, "מה אומר התקן על גישת ההכנסות?", [
        [call("search", query="גישת ההכנסות", document_ids=[world.guide], limit=None)],
        final("גישת ההכנסות מהוונת את ההכנסה של הנכס [S1].", claims=_claims("גישת ההכנסות מהוונת את ההכנסה"))],
        _resolution(intent=intent_part("professional", "topic", topic="גישת ההכנסות"), standalone_question="מה אומר התקן על גישת ההכנסות?"))
    second = _context(world, m2)
    assert second["property"] == first["property"] and second["params"] == first["params"]
    assert {s["document_id"] for s in second["sources"]} == {world.guide}  # the standards answer cites no property document
    assert world.rent not in {s["document_id"] for s in m2["answer"]["sources"]}
    inp = _next_input(world)
    assert inp.focus is not None and inp.focus["document_ids"] == [world.rent] and inp.focus["metric_kind"] == "rent"
    m3, _ = _turn(world, "ואיך זה מיושם בנכס הזה?", [
        [call("search", query="היוון", document_ids=[world.rent], limit=None)],
        final("הגישה מיושמת בנכס [S1].", focus=_focus(world.rent, SECRET_SUBJECT), claims=_claims("הגישה מיושמת בנכס"))],
        _resolution(relation="same_datum", standalone_question="איך מיישמים את גישת ההכנסות בנכס הזה?",
                    intent=intent_part("mixed", "conversation_property")))
    assert m3["answer"]["request"]["document_ids"] == [world.rent]
    assert _context(world, m3)["property"] == first["property"]


# --- 6. the period ---------------------------------------------------------------------------------------------------------------------


def set_switch(world, on: bool) -> None:
    """The conversation's "latest" switch (a conversation starts with it on)."""
    assert world.client.patch(f"/api/chat/conversations/{world.cid}", json={"prefer_recent": on}).status_code == 200


def time_of(world, m: dict) -> dict:
    return world.client.get(f"/api/chat/messages/{m['id']}/diagnostics").json()["resolution"]["time"]


def period_turn(world, question: str, *, new_question: bool = False, doc: str | None = None, intent=None):
    doc = doc or world.rent
    return _turn(world, question, [
        [call("search", query=question, document_ids=[doc], limit=None)],
        final("דמי השכירות הראויים נקבעו לפי הנכס [S1].", focus=_focus(doc, SUBJECT), claims=_claims("דמי השכירות הראויים נקבעו לפי הנכס"))],
        _resolution(relation="new_question" if new_question else "same_datum", standalone_question=question,
                    intent=intent or intent_part("property", "conversation_property"),
                    **({"changed_fields": [{"field": "subject", "user_words": "היסמין 3"}]} if new_question else {})))


def test_an_explicit_period_carries_to_a_follow_up_that_names_no_time_and_expires_on_a_property_change(world):
    set_switch(world, False)
    m1, _ = property_turn(world, "מה דמי השכירות הראויים ב-2022?", "דמי השכירות הראויים נקבעו לפי הנכס [S1].")
    first = _context(world, m1)["period"]
    assert first["requested"]["start"] == "2022" and first["source_message_id"] == m1["id"]
    assert time_of(world, m1)["source"] == "question"

    m2, agent = period_turn(world, "ומה היה היוון ההכנסה?")  # no time words, same property
    t = time_of(world, m2)
    assert (t["mode"], t["source"], t["carried_from"]) == ("explicit", "carried", m1["id"])
    assert "2022" in _task(agent) and "בשאלה קודמת" in _task(agent)
    assert _context(world, m2)["period"]["source_message_id"] == m1["id"]  # the question that stated it

    m3, _ = period_turn(world, "ומה היה ב-2024?")  # a new question that states time keeps its own
    t = time_of(world, m3)
    assert (t["source"], t["requested"]["start"], t.get("carried_from")) == ("question", "2024", None)
    assert _context(world, m3)["period"]["requested"]["start"] == "2024" and _context(world, m3)["period"]["source_message_id"] == m3["id"]

    m4, agent = _turn(world, "ומה דמי השכירות ברחוב היסמין 3?", [
        [call("search", query="דמי השכירות", document_ids=[world.jasmine], limit=None)],
        final("דמי השכירות נקבעו ברחוב היסמין [S1].", focus=_focus(world.jasmine, "היסמין 3"), claims=_claims("דמי השכירות נקבעו ברחוב היסמין"))],
        _resolution(relation="new_question", standalone_question="מה דמי השכירות ברחוב היסמין 3?",
                    changed_fields=[{"field": "subject", "user_words": "היסמין 3"}],
                    intent=intent_part("property", "named_entity")))
    assert m4["answer"]["request"]["entity_changed"] is True
    t = time_of(world, m4)
    assert (t["mode"], t["source"]) == ("none", "switch")  # the new property carries nothing of 2024
    assert "הנחיית זמן" not in _task(agent)
    ctx = _context(world, m4)
    assert ctx["period"] is None and ctx["property"]["document_ids"] == [world.jasmine]


def test_a_professional_turn_leaves_the_period_dormant_and_the_next_property_follow_up_still_carries_it(world):
    set_switch(world, False)
    m1, _ = property_turn(world, "מה דמי השכירות הראויים ב-2022?", "דמי השכירות הראויים נקבעו לפי הנכס [S1].")
    m2, _ = _turn(world, "מה אומר התקן על גישת ההכנסות?", [
        [call("search", query="גישת ההכנסות", document_ids=[world.guide], limit=None)],
        final("גישת ההכנסות מהוונת את ההכנסה של הנכס [S1].", claims=_claims("גישת ההכנסות מהוונת את ההכנסה"))],
        _resolution(intent=intent_part("professional", "topic", topic="גישת ההכנסות"), standalone_question="מה אומר התקן?"))
    assert time_of(world, m2)["mode"] == "none"  # a standards question runs on the switch
    assert _context(world, m2)["period"]["source_message_id"] == m1["id"]
    m3, _ = period_turn(world, "ובנכס הזה?")
    assert time_of(world, m3)["source"] == "carried" and time_of(world, m3)["carried_from"] == m1["id"]


@pytest.mark.parametrize("on", [True, False])
def test_with_the_latest_switch_on_an_explicit_period_still_wins_and_the_switch_decides_without_one(world, on):
    set_switch(world, on)
    m1, _ = property_turn(world, "מה דמי השכירות הראויים ב-2022?", "דמי השכירות הראויים נקבעו לפי הנכס [S1].")
    assert time_of(world, m1)["mode"] == "explicit"
    m2, agent = period_turn(world, "ומה היה היוון ההכנסה?")
    t = time_of(world, m2)
    assert (t["mode"], t["source"], t["prefer_recent"]) == ("explicit", "carried", on)  # not "recent", with the switch on
    assert "המשתמש ביקש להעדיף נתונים עדכניים" not in _task(agent) and "בשאלה קודמת" in _task(agent)
    m3, _ = _turn(world, "ומה דמי השכירות ברחוב היסמין 3?", [
        [call("search", query="דמי השכירות", document_ids=[world.jasmine], limit=None)],
        final("דמי השכירות נקבעו ברחוב היסמין [S1].", focus=_focus(world.jasmine, "היסמין 3"), claims=_claims("דמי השכירות נקבעו ברחוב היסמין"))],
        _resolution(relation="new_question", standalone_question="מה דמי השכירות ברחוב היסמין 3?",
                    changed_fields=[{"field": "subject", "user_words": "היסמין 3"}], intent=intent_part("property", "named_entity")))
    assert time_of(world, m3)["mode"] == ("recent" if on else "none")  # the property changed: the switch decides again


# --- 7. a list is gated by its own documents ---------------------------------------------------------------------------------------


def test_hiding_one_lists_document_drops_only_that_list_and_renumbers_nothing(world):
    older = "הנה רשימת הדיירים:\n1. **דייר ראשון** — חוזה [S1]\n2. **דייר שני** — חוזה [S1]"
    m1, _ = property_turn(world, "מי הדיירים בהיסמין 3?", older, doc=world.jasmine, subject="היסמין 3")
    (tenants,) = _context(world, m1)["lists"]
    assert tenants["document_ids"] == [world.jasmine]
    m2 = printed(world)
    plots, payments = [x for x in _context(world, m2)["lists"] if x["message_id"] == m2["id"]]
    carried = _context(world, m2)["lists"]
    assert [x["id"] for x in carried] == [plots["id"], payments["id"], tenants["id"]]

    with tenant_tx(world.system) as conn:
        conn.execute(text("UPDATE documents SET deleted_at = now() WHERE id = :d"), {"d": world.jasmine})
    inp = _next_input(world)
    ids = [x["id"] for x in inp.slots["lists"]]
    assert ids == [plots["id"], payments["id"]]  # only the tenants list is gone; the others keep their ids and place
    assert inp.slots["tombstones"] == [tenants["id"]]
    assert inp.slot_notes["lists"].startswith("dropped: " + tenants["id"])

    m3, _ = follow_up(world, "ומה לגבי התשלום השני?", intent_part("property", "prior_answer_item", item=2, list_id=payments["id"]))
    assert m3["answer"]["request"]["intent"]["item_label"] == "תשלום שני"
    assert tenants["id"] not in [x["id"] for x in _context(world, m3)["lists"]]


# --- 8. answers stored before the lists --------------------------------------------------------------------------------------------


def test_an_answer_stored_before_it_had_lists_resolves_through_the_legacy_reader(world):
    m1 = printed(world, "שלוש הגישות:\n1. **גישת ההשוואה** — עסקאות [S1]\n2. **גישת ההכנסות** — היוון [S1]\n3. **גישת העלות** — בנייה [S1]")
    with tenant_tx(world.system) as conn:  # an answer from before the slots: no context at all
        conn.execute(text("UPDATE messages SET answer = answer - 'context' WHERE id = :m"), {"m": m1["id"]})
    inp = _next_input(world)
    (entry,) = inp.slots["lists"]
    assert [i["label"] for i in entry["items"]] == ["גישת ההשוואה", "גישת ההכנסות", "גישת העלות"] and entry["document_ids"] == [world.rent]
    m2, _ = follow_up(world, "הסבר את הגישה השנייה", intent_part("property", "prior_answer_item", item=2))
    assert m2["answer"]["request"]["intent"]["item_label"] == "גישת ההכנסות"


def test_a_version_one_context_is_read_as_one_list(world):
    m1 = printed(world, "שלוש הגישות:\n1. **גישת ההשוואה** — עסקאות [S1]\n2. **גישת ההכנסות** — היוון [S1]\n3. **גישת העלות** — בנייה [S1]")
    with tenant_tx(world.system) as conn:
        conn.execute(text(
            "UPDATE messages SET answer = jsonb_set(answer - 'context', '{context}', CAST(:c AS jsonb)) WHERE id = :m"),
            {"m": m1["id"], "c": '{"version": 1, "topic": {"text": "גישות", "items": ["גישת ההשוואה", "גישת ההכנסות", "גישת העלות"],'
                                  ' "message_id": "m-0"}, "property": null, "params": null, "sources": [], "pending": null}'})
    (entry,) = _next_input(world).slots["lists"]
    assert [i["label"] for i in entry["items"]] == ["גישת ההשוואה", "גישת ההכנסות", "גישת העלות"] and entry["id"] == "m-0#1"
