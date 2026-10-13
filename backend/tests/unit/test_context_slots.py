"""The conversation's slots, the items a list answer printed, and the rules that let a message bind to a pending
clarification or to an item of the previous answer (round 10 U7; R10-R15, KTD4-KTD7).

Pure functions, on synthetic labels and invented documents: the model's parse is given here, so these tests prove what
the server makes of it."""

from __future__ import annotations

import pytest

from app.chat import entities, resolve, slots
from app.chat.intent import IntentClaim, settle
from app.chat.resolve import ResolvedRequest, validate

VOCABULARY = ["דירת מגורים רחוב התמר 9 עיר-ב (סינתטי)", "דירה ברחוב היסמין 3 עיר-ג (סינתטי)", "מדריך שמאות כללי (סינתטי)"]


# --- the items a list answer printed (KTD6) ----------------------------------------------------------------------------


def test_the_items_of_a_numbered_list_are_labelled_by_their_bold_lead():
    md = ("שלוש הגישות:\n1. **גישת ההשוואה** — השוואה לעסקאות דומות [S1]\n2. **גישת ההכנסות**: היוון ההכנסה [S2]\n"
          "3. **גישת העלות** — עלות הבנייה\n\nבסיכום, אפשר לשלב ביניהן.")
    assert slots.extract_items(md) == ["גישת ההשוואה", "גישת ההכנסות", "גישת העלות"]


def test_an_item_without_a_bold_lead_is_labelled_by_the_text_before_a_dash_or_a_colon():
    md = "- גישת ההשוואה — מבוססת על עסקאות\n- גישת ההכנסות: מהוונת הכנסה\n* גישת העלות\n• גישה רביעית [S3]"
    assert slots.extract_items(md) == ["גישת ההשוואה", "גישת ההכנסות", "גישת העלות", "גישה רביעית"]


def test_only_top_level_items_are_taken_and_a_nested_list_is_not_an_item():
    md = "1. **גישת ההשוואה**\n   - עסקאות\n   - התאמות\n2. **גישת ההכנסות**\n    1. היוון\n3. **גישת העלות**"
    assert slots.extract_items(md) == ["גישת ההשוואה", "גישת ההכנסות", "גישת העלות"]


def test_a_single_item_is_no_list_and_text_without_a_list_has_no_items():
    assert slots.extract_items("1. **גישת ההשוואה**") == []
    assert slots.extract_items("גישת ההשוואה מבוססת על עסקאות דומות. גישת ההכנסות מהוונת הכנסה.") == []
    assert slots.extract_items("") == []


def test_the_first_list_of_the_answer_is_the_one_and_a_numbered_list_is_preferred_to_bullets():
    md = "- א ראשון\n- ב שני\n\nואחר כך:\n1. **גישה א**\n2. **גישה ב**\n\n- ג\n- ד"
    assert slots.extract_items(md) == ["גישה א", "גישה ב"]
    assert slots.extract_items("- ראשון\n- שני\n\nטקסט\n- שלישי\n- רביעי") == ["ראשון", "שני"]


def test_items_are_capped_in_count_and_length_and_carry_no_markup_or_citations():
    md = "\n".join(f"{n}. **פריט {n}** — `x` [S1]" for n in range(1, 15))
    assert slots.extract_items(md) == [f"פריט {n}" for n in range(1, 11)]
    (long,) = slots.extract_items("1. " + "מ" * 200 + "\n2. קצר")[:1]
    assert len(long) <= slots.ITEM_CHARS
    assert slots.extract_items("1. _גישה_ \\[S2\\]\n2. **b** [S1][S2]") == ["גישה", "b"]


# --- an ordinal that binds to an item (KTD6) -----------------------------------------------------------------------------

ITEMS = ["גישת ההשוואה", "גישת ההכנסות", "גישת העלות"]


@pytest.mark.parametrize("message, expected", [
    ("הסבר את הגישה השנייה", {2}), ("ואת השלישית", {3}), ("הראשון", {1}), ("הסבר את 3", {3}), ("פריט 2 בבקשה", {2}),
    ("האחרונה", {-1}), ("בשנייה", {2}), ("ומה היה השווי ב-2024?", set()), ("הסבר אותה", set()),
    ("כמה עולה 3.5 מ\"ר?", set()), ("השניים", set())])
def test_the_ordinal_forms_a_message_holds(message, expected):
    assert slots.ordinal_forms(message) == expected


def test_an_item_is_accepted_only_with_an_ordinal_form_in_the_message_and_an_item_that_exists():
    assert slots.check_item(2, "הסבר את הגישה השנייה", ITEMS) is None
    assert slots.check_item(3, "ואת האחרונה?", ITEMS) is None  # "the last" is the item the list ends with
    assert "list" in slots.check_item(2, "הסבר את הגישה השנייה", [])
    assert "ordinal" in slots.check_item(2, "הסבר אותה", ITEMS)
    assert "ordinal" in slots.check_item(2, "ומה היה השווי ב-2024?", ITEMS)
    assert "exist" in slots.check_item(4, "הסבר את הגישה הרביעית", ITEMS)
    assert "exist" in slots.check_item(None, "הסבר את הגישה השנייה", ITEMS)
    assert "contradict" in slots.check_item(3, "הסבר את הגישה השנייה", ITEMS)  # not the item the words name
    assert "contradict" in slots.check_item(1, "ואת האחרונה", ITEMS)


def claim(value: str = "professional", reference: str = "none", item: int | None = None, topic: str = "") -> IntentClaim:
    return IntentClaim(intent=value, reference=reference, item=item, topic=topic, standard_refs=[])


def test_settle_binds_a_valid_item_and_drops_an_invalid_one_with_the_reason():
    ok = settle(claim(reference="prior_answer_item", item=2), "הסבר את הגישה השנייה", VOCABULARY, items=ITEMS)
    assert ok.item == 2 and ok.item_label == "גישת ההכנסות" and ok.reference == "prior_answer_item" and ok.bound == "item"
    bad = settle(claim(reference="prior_answer_item", item=2), "הסבר אותה", VOCABULARY, items=ITEMS)
    assert bad.item is None and bad.reference == "none" and not bad.no_list
    assert any(r.startswith("prior_answer_item rejected") for r in bad.reasons)
    nothing = settle(claim(reference="prior_answer_item", item=2), "הסבר את הגישה השנייה", VOCABULARY)
    assert nothing.item is None and nothing.no_list  # an ordinal was written and no list was given
    unrelated = settle(claim(reference="none", item=2), "הסבר את הגישה השנייה", VOCABULARY, items=ITEMS)
    assert unrelated.item is None  # an item is meaningful only for the reference that names it


def test_settle_binds_a_pronoun_to_the_topic_or_the_property_only_when_the_slot_exists():
    assert settle(claim(reference="topic"), "איך מיישמים אותה?", VOCABULARY, topic="גישות השומה").bound == "topic"
    assert settle(claim(reference="topic"), "איך מיישמים אותה?", VOCABULARY).bound == ""
    assert settle(claim("property", "conversation_property"), "ומה השווי שם?", VOCABULARY, has_property=True).bound == "property"
    nowhere = settle(claim("property", "conversation_property"), "ומה השווי שם?", VOCABULARY)
    assert nowhere.bound == "" and any("property" in r for r in nowhere.reasons)


# --- the slots each answer carries (KTD4) ------------------------------------------------------------------------------

PROPERTY = {"document_ids": ["d-1"], "located_unit": {"document_id": "d-1", "unit_no": 2, "display": "מתחם הדולב"},
            "subject": "מתחם הדולב"}
PARAMS = {"metric_kind": "value", "unit": "ILS", "period": "unknown", "vat": "unknown", "area_basis": ""}
SOURCE = {"document_id": "d-1", "source_kind": "appraisal", "standard": None, "section": None}


def test_a_property_turn_writes_the_property_and_its_parameters():
    out = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, sources=[SOURCE], message_id="m-1")
    assert out["version"] == slots.VERSION == 1
    assert out["property"] == PROPERTY and out["params"] == PARAMS and out["sources"] == [SOURCE]
    assert out["topic"] is None and out["pending"] is None


def test_a_professional_turn_carries_the_property_and_the_parameters_forward_unchanged_and_writes_the_topic():
    before = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, message_id="m-1")
    out = slots.carry(before, intent="professional", property={"document_ids": ["other"]}, params={"metric_kind": "rent"},
                      topic="גישות השומה", items=ITEMS, message_id="m-2", sources=[SOURCE | {"document_id": "g"}])
    assert out["property"] == PROPERTY and out["params"] == PARAMS  # whatever this turn found, the slot is dormant
    assert out["topic"] == {"text": "גישות השומה", "items": ITEMS, "message_id": "m-2"}
    assert out["sources"] == [SOURCE | {"document_id": "g"}]  # each turn writes what it cited


def test_topic_items_persist_until_a_later_answer_lists_new_ones():
    first = slots.carry(None, intent="professional", topic="גישות השומה", items=ITEMS, message_id="m-1")
    second = slots.carry(first, intent="professional", topic="גישת ההכנסות", message_id="m-2")
    assert second["topic"] == {"text": "גישת ההכנסות", "items": ITEMS, "message_id": "m-1"}
    third = slots.carry(second, intent="property", property=PROPERTY, params=PARAMS, message_id="m-3")
    assert third["topic"] == second["topic"]  # a property turn leaves the topic as it was
    fourth = slots.carry(third, intent="professional", items=["א", "ב"], message_id="m-4")
    assert fourth["topic"] == {"text": "גישת ההכנסות", "items": ["א", "ב"], "message_id": "m-4"}


def test_a_mixed_turn_writes_the_property_and_the_topic():
    before = slots.carry(None, intent="professional", topic="גישות השומה", items=ITEMS, message_id="m-1")
    out = slots.carry(before, intent="mixed", property=PROPERTY, params=PARAMS, topic="גישות השומה", message_id="m-2")
    assert out["property"] == PROPERTY and out["topic"]["items"] == ITEMS


def test_a_property_turn_without_a_property_leaves_none_and_pending_is_only_this_turns():
    before = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, message_id="m-1")
    pending = {"kind": "candidates", "payload": {"candidates": []}, "asked_at": "2026-10-13T10:00:00"}
    asked = slots.carry(before, intent="property", pending=pending, message_id="m-2")
    assert asked["property"] is None and asked["pending"] == pending
    assert slots.carry(asked, intent="professional", message_id="m-3")["pending"] is None  # one message only


def test_a_dropped_property_slot_is_not_resurrected_by_a_professional_turn():
    dropped = {"version": 1, "topic": None, "property": None, "params": None, "sources": [], "pending": None}
    assert slots.carry(dropped, intent="professional", topic="x", message_id="m")["property"] is None


def test_items_and_texts_are_capped_when_written():
    out = slots.carry(None, intent="professional", topic="נ" * 500, items=["א" * 200] * 20, message_id="m")
    assert len(out["topic"]["items"]) == slots.MAX_ITEMS and all(len(i) <= slots.ITEM_CHARS for i in out["topic"]["items"])
    assert len(out["topic"]["text"]) <= slots.TOPIC_CHARS


def test_a_focus_splits_into_the_property_and_its_parameters_and_joins_back():
    focus = {"metric_as_written": "שווי", "metric_kind": "value", "unit": "ILS", "period": "unknown", "area_basis": "",
             "vat": "unknown", "subject": "מתחם הדולב", "value_role": "appraiser_determination", "document_ids": ["d-1"],
             "located_unit": PROPERTY["located_unit"]}
    prop, params = slots.split_focus(focus)
    assert prop == {"document_ids": ["d-1"], "located_unit": PROPERTY["located_unit"], "subject": "מתחם הדולב"}
    assert params["metric_kind"] == "value" and "document_ids" not in params and "subject" not in params
    assert slots.join_focus({"property": prop, "params": params}) == focus
    assert slots.split_focus(None) == (None, None) and slots.join_focus({"property": None, "params": PARAMS}) is None
    bare = slots.join_focus({"property": prop, "params": None})
    assert bare["metric_kind"] == "unknown" and bare["document_ids"] == ["d-1"] and bare["subject"] == "מתחם הדולב"


# --- a pending clarification binds only the message that answers it (KTD5) ---------------------------------------------


@pytest.mark.parametrize("message, bound", [("8%", True), (" 8 % ", True), ("כ-8 אחוז", True), ("1,500,000 ₪", True),
                                            ("8.000000000000000000000000%", True),  # no length cap
                                            ("ומה היה השווי ב-2024?", False), ("דירה 4", False), ("", False)])
def test_a_message_of_a_number_alone_is_a_reply_whatever_its_length(message, bound):
    assert resolve.number_only_reply(message) is bound
    assert resolve.reply_binds(None, message) is bound  # whether or not the resolve call succeeded


def _request(relation: str, intent: str) -> resolve.Request:
    from app.chat.intent import Intent

    return resolve.Request("clarification_answer", "?", relation=relation, intent=Intent(value=intent))


def test_a_parameter_reply_binds_for_a_clarification_answer_that_is_not_professional_or_a_number_alone():
    words = "נניח שהעלויות יעלו ב-8 אחוז"
    assert resolve.reply_binds(_request("clarification_answer", "property"), words)
    assert resolve.reply_binds(_request("clarification_answer", "unknown"), words)
    assert not resolve.reply_binds(_request("clarification_answer", "professional"), words)
    assert not resolve.reply_binds(_request("new_question", "property"), words)
    assert not resolve.reply_binds(None, words)  # a failed resolve: only a number alone is surely the answer
    assert resolve.reply_binds(_request("new_question", "professional"), "8%")  # a number alone, even so


# --- candidates (ordinals and names) ------------------------------------------------------------------------------------

CANDIDATES = [
    {"document_id": "d-1", "title": "מתחם הגפן א", "file_title": "קובץ",
     "unit": {"document_id": "d-1", "unit_no": 1, "title": "מתחם הגפן א", "display": "מתחם הגפן א", "aliases": [],
              "identifiers": [], "pages": [1, 1]}},
    {"document_id": "d-1", "title": "מתחם הגפן ב", "file_title": "קובץ",
     "unit": {"document_id": "d-1", "unit_no": 2, "title": "מתחם הגפן ב", "display": "מתחם הגפן ב", "aliases": [],
              "identifiers": [], "pages": [2, 2]}},
]


@pytest.mark.parametrize("reply, expected", [("השנייה", True), ("את השנייה", True), ("2", True), ("מספר 2", True),
                                             ("התכוונתי לשנייה", True), ("הראשון", True), ("כולן", False),
                                             ("הסבר את הגישה השנייה", False), ("מה זה שווי שוק?", False), ("", False)])
def test_a_reply_that_is_an_ordinal_or_a_list_number_alone(reply, expected):
    assert entities.ordinal_reply(reply) is expected


def test_a_reply_names_several_candidates_only_when_a_word_it_holds_is_held_by_two_or_more_of_them():
    allowed = {"d-1"}
    assert entities.names_several(CANDIDATES, "הגפן", allowed)
    assert entities.names_several(CANDIDATES, "בהגפן", allowed)
    assert not entities.names_several(CANDIDATES, "כולן", allowed)
    assert not entities.names_several(CANDIDATES, "מה זה שווי שוק?", allowed)
    assert not entities.names_several(CANDIDATES, "מתחם הגפן ב", allowed)
    assert not entities.names_several(CANDIDATES, "הגפן", set())


def _resolved(**kw) -> ResolvedRequest:
    base = dict(relation="new_question", scope="entity", standalone_question="?", changed_fields=[], metric_kind="unknown",
                unit="unknown", scale="unknown", period="unknown", area_basis="", vat="unknown", subject="",
                document_ids=[], ambiguity="")
    return ResolvedRequest(**(base | kw))


def _intent(value: str, reference: str = "none", item: int | None = None, topic: str = "") -> dict:
    return {"intent": value, "reference": reference, "item": item, "topic": topic, "standard_refs": []}


def _validate(resolved, message, focus=None, candidates=None, items=None, listing=None, topic=""):
    return validate(resolved, focus, message, lambda ids: set(ids), VOCABULARY, None, candidates, items=items,
                    listing=listing, topic=topic)


def test_an_ordinal_reply_picks_the_candidate_whatever_the_parse_calls_the_message():
    for relation in ("clarification_answer", "new_question"):
        req = _validate(_resolved(relation=relation, intent=_intent("property", "clarification")), "השנייה",
                        candidates=CANDIDATES)
        assert req.located_unit["unit_no"] == 2 and req.resolution["decisions"]["pending"].startswith("bound")


def test_a_message_that_is_not_an_answer_is_a_new_question_and_the_pending_clarification_expires():
    for resolved, message in [
            (_resolved(intent=_intent("professional", "topic")), "מה זה שווי שוק?"),
            (_resolved(intent=_intent("professional", "none")), "השנייה"),  # a professional intent binds nothing
            (_resolved(relation="new_question", intent=_intent("property")), "מה זה שווי שוק?"),
            (_resolved(relation="clarification_answer", intent=_intent("property", "clarification")), "כולן")]:
        req = _validate(resolved, message, candidates=CANDIDATES)
        assert not req.clarify and req.document_ids == [] and req.located_unit is None, message
        assert req.resolution["decisions"]["pending"].startswith("expired"), message


def test_the_clarification_is_asked_again_only_for_an_answer_that_names_two_or_more_candidates():
    req = _validate(_resolved(relation="clarification_answer", intent=_intent("property", "clarification")), "הגפן",
                    candidates=CANDIDATES)
    assert req.server_clarify and len(req.candidates) == 2
    new = _validate(_resolved(relation="new_question", intent=_intent("property")), "הגפן", candidates=CANDIDATES)
    assert not new.clarify  # not called an answer: a new question


def test_a_complete_name_stated_in_a_new_question_still_selects_that_candidate():
    req = _validate(_resolved(relation="new_question", intent=_intent("property", "named_entity")), "מתחם הגפן ב",
                    candidates=CANDIDATES)
    assert req.located_unit["unit_no"] == 2


# --- items, topic and the property in the request (KTD6, KTD7) ------------------------------------------------------------


def test_the_standalone_question_names_the_item_an_ordinal_binds_to():
    r = _resolved(standalone_question="הסבר את הגישה השנייה", intent=_intent("professional", "prior_answer_item", item=2))
    req = _validate(r, "הסבר את הגישה השנייה", items=ITEMS)
    assert req.intent.item == 2 and req.topic == "גישת ההכנסות" and "גישת ההכנסות" in req.standalone_question
    assert req.resolution["decisions"]["item"].startswith("bound: 2")
    assert req.document_ids == [] and req.intent.value == "professional"
    named = _resolved(standalone_question="הסבר את גישת ההכנסות", intent=_intent("professional", "prior_answer_item", item=2))
    assert _validate(named, "הסבר את השנייה", items=ITEMS).standalone_question == "הסבר את גישת ההכנסות"


def test_an_ordinal_with_no_list_is_rejected_recorded_and_flagged_for_the_requested_block():
    r = _resolved(intent=_intent("professional", "prior_answer_item", item=2))
    req = _validate(r, "הסבר את הגישה השנייה")
    assert req.intent.item is None and req.intent.no_list
    assert req.resolution["decisions"]["item"].startswith("rejected")
    assert "לא ניתנה רשימה" in resolve.requested_block(req)
    ok = _validate(_resolved(intent=_intent("professional", "prior_answer_item", item=2)), "הסבר את הגישה השנייה", items=ITEMS)
    assert "לא ניתנה רשימה" not in resolve.requested_block(ok) and "גישת ההכנסות" in resolve.requested_block(ok)


def test_an_ordinal_that_also_fits_the_candidates_or_a_listing_gets_one_focused_server_question():
    r = _resolved(intent=_intent("professional", "prior_answer_item", item=2))
    both = _validate(r, "השנייה", candidates=CANDIDATES, items=ITEMS)
    assert both.server_clarify and "גישת ההכנסות" in both.clarify and "מתחם הגפן ב" in both.clarify
    assert both.candidates == [] and both.resolution["decisions"]["item"].startswith("ambiguous")
    listing = [{"document_id": "d-1", "title": "שומה א"}, {"document_id": "d-2", "title": "שומה ב"}]
    listed = _validate(r, "השנייה", items=ITEMS, listing=listing)
    assert listed.server_clarify and "שומה ב" in listed.clarify and "ברשימת המסמכים" in listed.clarify
    same = _validate(r, "השנייה", items=ITEMS, listing=[{"document_id": "d-1", "title": "גישת ההשוואה"},
                                                        {"document_id": "d-2", "title": "גישת ההכנסות"}])
    assert not same.clarify  # the listing and the list name the same thing: one reading
    short = _validate(_resolved(intent=_intent("professional", "prior_answer_item", item=3)), "השלישית",
                      candidates=CANDIDATES, items=ITEMS)
    assert not short.clarify  # the third fits no candidate: one reading


FOCUS = {"metric_as_written": "שווי", "metric_kind": "value", "unit": "ILS", "period": "unknown", "area_basis": "",
         "vat": "unknown", "subject": "הנכס הנוכחי", "value_role": "appraiser_determination", "document_ids": ["d-1"]}


def test_a_mixed_question_about_this_property_restores_the_property_and_binds_the_topic():
    r = _resolved(intent=_intent("mixed", "topic"), standalone_question="איך מיישמים אותה בנכס הזה?")
    req = _validate(r, "איך מיישמים אותה בנכס הזה?", focus=FOCUS, topic="גישות השומה")
    assert req.intent.value == "mixed" and req.document_ids == ["d-1"] and req.subject == "הנכס הנוכחי"
    assert req.topic == "גישות השומה" and req.resolution["decisions"]["documents"].startswith("reinstated")
    assert req.resolution["decisions"]["topic"].startswith("bound")


def test_a_pronoun_with_no_topic_stored_binds_to_nothing():
    req = _validate(_resolved(intent=_intent("professional", "topic", topic="שווי שוק")), "איך מיישמים אותה?")
    assert req.topic == "שווי שוק"  # the model's own words for the topic stand
    assert req.resolution["decisions"]["topic"].startswith("no topic")


def test_a_professional_question_never_reinstates_the_property_and_a_property_question_with_no_reference_does_not():
    prof = _validate(_resolved(intent=_intent("professional", "topic")), "מה זה שווי שוק?", focus=FOCUS)
    assert prof.document_ids == []
    plain = _validate(_resolved(intent=_intent("property", "none")), "מה זה כתוב על הגג?", focus=FOCUS)
    assert plain.document_ids == []  # a new question with no reference to the conversation's property
    pointed = _validate(_resolved(intent=_intent("property", "conversation_property")), "ומה השווי שם?", focus=FOCUS)
    assert pointed.document_ids == ["d-1"]


def test_the_requested_block_carries_the_topic_and_the_item():
    r = _resolved(intent=_intent("professional", "prior_answer_item", item=2))
    req = _validate(r, "הסבר את הגישה השנייה", items=ITEMS)
    block = resolve.requested_block(req)
    assert "פריט 2" in block and "«גישת ההכנסות»" in block
