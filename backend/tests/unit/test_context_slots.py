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


def test_items_are_capped_in_length_and_carry_no_markup_or_citations():
    md = "\n".join(f"{n}. **פריט {n}** — `x` [S1]" for n in range(1, 15))
    assert slots.extract_items(md) == [f"פריט {n}" for n in range(1, 15)]  # round 22 U12: a list of 14 is not cut at 10
    (long,) = slots.extract_items("1. " + "מ" * 200 + "\n2. קצר")[:1]
    assert len(long) <= slots.ITEM_CHARS
    assert slots.extract_items("1. _גישה_ \\[S2\\]\n2. **b** [S1][S2]") == ["גישה", "b"]


# --- an ordinal that binds to an item (KTD6) -----------------------------------------------------------------------------

ITEMS = ["גישת ההשוואה", "גישת ההכנסות", "גישת העלות"]


def a_list(labels, title="", kind="", mid="m-0", docs=(), total=None, n=1, age=0):
    """A list as the conversation stores it (``slots.stamp``), with the id ``<mid>#<n>``."""
    (entry,) = slots.stamp([{"title": title, "kind": kind or slots._kind(list(labels)), "items": [{"label": x} for x in labels],
                             "total": total or len(labels), "document_ids": list(docs)}], mid)
    return entry | {"id": f"{mid}#{n}", "age": age}


APPROACH_LISTS = [a_list(ITEMS, "שלוש הגישות")]


@pytest.mark.parametrize("message, expected", [
    ("הסבר את הגישה השנייה", {2}), ("ואת השלישית", {3}), ("הראשון", {1}), ("הסבר את 3", {3}), ("פריט 2 בבקשה", {2}),
    ("האחרונה", {-1}), ("בשנייה", {2}), ("ומה היה השווי ב-2024?", set()), ("הסבר אותה", set()),
    ("כמה עולה 3.5 מ\"ר?", set()), ("השניים", set()), ("ומה לגבי המגרש ה-12?", {12}), ("מה השטח ל-5?", {5})])
def test_the_ordinal_forms_a_message_holds(message, expected):
    assert slots.ordinal_forms(message, 15) == expected  # a list of 15 is in scope


@pytest.mark.parametrize("message", [
    "מה השווי ב-2012?", "מה השטח בגוש 6012?", "מה השטח בגוש 6012 חלקה 12?", "מה התשואה, 12%?", "מה היה ב-12/5?", "מה השטח בקומה 12?",
    "כמה זה 12 מ\"ר?", "מה השווי ל-5 שנים?", "מה עלה תוספת 12%?", "מה אומר תקן 12?", "מה אומר סעיף 5?", "ב-12", "מה השווי ב-2012 וב-2013?"])
def test_a_number_that_is_not_a_place_is_never_read_as_one_even_with_a_list_in_scope(message):
    """N3: years, block and parcel numbers, percentages, dates, floors, areas, standards and durations are not places in a list."""
    assert slots.ordinal_forms(message, 15) == set(), message


def test_a_number_is_read_as_a_place_only_when_a_list_that_long_is_in_scope():
    assert slots.ordinal_forms("ומה לגבי המגרש ה-12?") == set()  # no list in scope
    assert slots.ordinal_forms("ומה לגבי המגרש ה-12?", 11) == set()  # the list is shorter than the number
    assert slots.ordinal_forms("ומה לגבי המגרש ה-12?", 12) == {12}
    assert slots.ordinal_forms("הסבר את הגישה השנייה") == {2}  # an ordinal word needs no list to be read


def test_an_item_is_accepted_only_with_an_ordinal_form_in_the_message_and_an_item_that_exists():
    b = slots.bind
    assert b("הסבר את הגישה השנייה", APPROACH_LISTS, item=2).items == (2,)
    assert b("ואת האחרונה?", APPROACH_LISTS, item=3).items == (3,)  # "the last" is the item the list ends with
    assert b("הסבר את הגישה השנייה", []).no_list and "list" in b("הסבר את הגישה השנייה", [], item=2).reason
    assert "ordinal" in b("הסבר אותה", APPROACH_LISTS, item=2).reason
    assert "ordinal" in b("ומה היה השווי ב-2024?", APPROACH_LISTS, item=2).reason
    assert "exist" in b("הסבר את הגישה הרביעית", APPROACH_LISTS, item=4).reason
    assert "exist" in b("הסבר את הגישה השנייה", APPROACH_LISTS, item=None).reason
    assert "contradict" in b("הסבר את הגישה השנייה", APPROACH_LISTS, item=3).reason  # not the item the words name
    assert "contradict" in b("ואת האחרונה", APPROACH_LISTS, item=1).reason


def claim(value: str = "professional", reference: str = "none", item: int | None = None, topic: str = "") -> IntentClaim:
    return IntentClaim(intent=value, reference=reference, item=item, topic=topic, standard_refs=[])


def test_settle_binds_a_valid_item_and_drops_an_invalid_one_with_the_reason():
    ok = settle(claim(reference="prior_answer_item", item=2), "הסבר את הגישה השנייה", VOCABULARY, lists=APPROACH_LISTS)
    assert ok.item == 2 and ok.item_label == "גישת ההכנסות" and ok.reference == "prior_answer_item" and ok.bound == "item"
    bad = settle(claim(reference="prior_answer_item", item=2), "הסבר אותה", VOCABULARY, lists=APPROACH_LISTS)
    assert bad.item is None and bad.reference == "none" and not bad.no_list
    assert any(r.startswith("prior_answer_item rejected") for r in bad.reasons)
    nothing = settle(claim(reference="prior_answer_item", item=2), "הסבר את הגישה השנייה", VOCABULARY)
    assert nothing.item is None and nothing.no_list  # an ordinal was written and no list was given
    unrelated = settle(claim(reference="none", item=2), "הסבר את הגישה השנייה", VOCABULARY, lists=APPROACH_LISTS)
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


PRINTED = [{"title": "שלוש הגישות", "kind": "גישת", "items": [{"label": x} for x in ITEMS], "total": 3, "document_ids": []}]


def test_a_property_turn_writes_the_property_and_its_parameters():
    out = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, sources=[SOURCE], message_id="m-1")
    assert out["version"] == slots.VERSION == 2
    assert out["property"] == PROPERTY and out["params"] == PARAMS and out["sources"] == [SOURCE]
    assert out["topic"] is None and out["pending"] is None and out["lists"] == [] and out["period"] is None


def test_a_professional_turn_carries_the_property_and_the_parameters_forward_unchanged_and_writes_the_topic():
    before = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, message_id="m-1")
    out = slots.carry(before, intent="professional", property={"document_ids": ["other"]}, params={"metric_kind": "rent"},
                      topic="גישות השומה", lists=PRINTED, message_id="m-2", sources=[SOURCE | {"document_id": "g"}])
    assert out["property"] == PROPERTY and out["params"] == PARAMS  # whatever this turn found, the slot is dormant
    assert out["topic"] == {"text": "גישות השומה", "message_id": "m-2"}
    assert [x["label"] for x in out["lists"][0]["items"]] == ITEMS and out["lists"][0]["id"] == "m-2#1"
    assert out["sources"] == [SOURCE | {"document_id": "g"}]  # each turn writes what it cited


def test_lists_persist_until_they_age_out_and_a_new_list_does_not_replace_an_older_one():
    first = slots.carry(None, intent="professional", topic="גישות השומה", lists=PRINTED, message_id="m-1")
    second = slots.carry(first, intent="professional", topic="גישת ההכנסות", message_id="m-2")
    assert second["topic"] == {"text": "גישת ההכנסות", "message_id": "m-2"}
    assert [(x["id"], x["age"]) for x in second["lists"]] == [("m-1#1", 1)]
    third = slots.carry(second, intent="property", property=PROPERTY, params=PARAMS, message_id="m-3")
    assert [(x["id"], x["age"]) for x in third["lists"]] == [("m-1#1", 2)]  # a property turn leaves the lists as they were
    fourth = slots.carry(third, intent="professional", lists=[{"items": [{"label": "א"}, {"label": "ב"}]}], message_id="m-4")
    # the list of m-1 was printed three answers ago: it is past the last six messages
    assert [(x["id"], x["age"]) for x in fourth["lists"]] == [("m-4#1", 0)]


def test_at_most_three_lists_are_carried_newest_first_and_an_id_is_never_a_place():
    one = [{"items": [{"label": "א"}, {"label": "ב"}], "title": f"רשימה {n}"} for n in range(1, 3)]
    out = slots.carry(None, intent="property", lists=one, message_id="m-1")
    out = slots.carry(out, intent="property", lists=one, message_id="m-2")
    assert [x["id"] for x in out["lists"]] == ["m-2#1", "m-2#2", "m-1#1"]  # three, the newest first
    kept, dropped = slots.gate_lists(out, {"never-matches"} | slots.list_documents(out))
    assert dropped == [] and kept == out  # no documents: nothing to hide


def test_a_property_turn_without_a_property_leaves_none_and_pending_is_only_this_turns():
    before = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, message_id="m-1")
    pending = {"kind": "candidates", "payload": {"candidates": []}, "asked_at": "2026-10-13T10:00:00"}
    asked = slots.carry(before, intent="property", pending=pending, message_id="m-2")
    assert asked["property"] is None and asked["pending"] == pending
    assert slots.carry(asked, intent="professional", message_id="m-3")["pending"] is None  # one message only


def test_a_dropped_property_slot_is_not_resurrected_by_a_professional_turn():
    dropped = {"version": 2, "topic": None, "property": None, "params": None, "sources": [], "pending": None}
    assert slots.carry(dropped, intent="professional", topic="x", message_id="m")["property"] is None


def test_items_titles_and_texts_are_capped_when_written_and_the_total_is_kept():
    big = [{"items": [{"label": "א" * 200}] * 50, "title": "כ" * 300, "total": 60}]
    out = slots.carry(None, intent="professional", topic="נ" * 500, lists=big, message_id="m")
    entry = out["lists"][0]
    assert len(entry["items"]) == slots.MAX_LIST_ITEMS and all(len(i["label"]) <= slots.ITEM_CHARS for i in entry["items"])
    assert entry["total"] == 60 and len(entry["title"]) <= slots.TITLE_CHARS
    assert len(out["topic"]["text"]) <= slots.TOPIC_CHARS


def test_the_period_belongs_to_the_property_and_a_professional_turn_leaves_it_dormant():
    period = {"requested": {"kind": "point", "start": "2022", "end": "2022", "precision": "year", "text": "ב-2022"},
              "source_message_id": "m-1"}
    first = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, period=period, message_id="m-1")
    assert first["period"] == period
    prof = slots.carry(first, intent="professional", period={"requested": {}}, message_id="m-2")
    assert prof["period"] == period  # whatever the standards question stated, the property's period is dormant
    moved = slots.carry(prof, intent="property", property=PROPERTY | {"document_ids": ["d-9"]}, params=PARAMS, message_id="m-3")
    assert moved["period"] is None  # a property turn that states no time carries none of its own
    assert slots.carry(None, intent="property", period=period, message_id="m-1")["period"] is None  # no property, no period


def test_a_context_of_version_one_is_read_as_one_list():
    old = {"version": 1, "topic": {"text": "גישות השומה", "items": ITEMS, "message_id": "m-1"}, "property": None, "params": None,
           "sources": [SOURCE], "pending": None}
    out = slots.upgrade_v1(old)
    assert out["version"] == 2 and out["period"] is None and out["topic"] == {"text": "גישות השומה", "message_id": "m-1"}
    (entry,) = out["lists"]
    assert [i["label"] for i in entry["items"]] == ITEMS and entry["document_ids"] == ["d-1"] and entry["id"] == "m-1#1"
    assert slots.upgrade_v1(old | {"topic": None})["lists"] == []


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


def _validate(resolved, message, focus=None, candidates=None, lists=None, listing=None, topic=""):
    return validate(resolved, focus, message, lambda ids: set(ids), VOCABULARY, None, candidates, lists=lists,
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


def test_the_candidates_reach_the_model_again_only_for_an_answer_that_names_two_or_more_of_them():
    req = _validate(_resolved(relation="clarification_answer", intent=_intent("property", "clarification")), "הגפן",
                    candidates=CANDIDATES)
    # round 15 U6: the two candidates the reply names reach the model again; the server asks nothing
    assert not req.server_clarify and not req.clarify and len(req.offered) == 2 and req.candidates == []
    new = _validate(_resolved(relation="new_question", intent=_intent("property")), "הגפן", candidates=CANDIDATES)
    assert not new.clarify and not new.offered  # not called an answer: a new question


def test_a_complete_name_stated_in_a_new_question_still_selects_that_candidate():
    req = _validate(_resolved(relation="new_question", intent=_intent("property", "named_entity")), "מתחם הגפן ב",
                    candidates=CANDIDATES)
    assert req.located_unit["unit_no"] == 2


# --- items, topic and the property in the request (KTD6, KTD7) ------------------------------------------------------------


def test_the_standalone_question_names_the_item_an_ordinal_binds_to():
    r = _resolved(standalone_question="הסבר את הגישה השנייה", intent=_intent("professional", "prior_answer_item", item=2))
    req = _validate(r, "הסבר את הגישה השנייה", lists=APPROACH_LISTS)
    assert req.intent.item == 2 and req.topic == "גישת ההכנסות" and "גישת ההכנסות" in req.standalone_question
    assert req.resolution["decisions"]["item"].startswith("bound: 2")
    assert req.document_ids == [] and req.intent.value == "professional"
    named = _resolved(standalone_question="הסבר את גישת ההכנסות", intent=_intent("professional", "prior_answer_item", item=2))
    assert _validate(named, "הסבר את השנייה", lists=APPROACH_LISTS).standalone_question == "הסבר את גישת ההכנסות"


def test_an_ordinal_with_no_list_is_rejected_recorded_and_flagged_for_the_requested_block():
    r = _resolved(intent=_intent("professional", "prior_answer_item", item=2))
    req = _validate(r, "הסבר את הגישה השנייה")
    assert req.intent.item is None and req.intent.no_list
    assert req.resolution["decisions"]["item"].startswith("rejected")
    assert "לא ניתנה רשימה" in resolve.requested_block(req)
    ok = _validate(_resolved(intent=_intent("professional", "prior_answer_item", item=2)), "הסבר את הגישה השנייה", lists=APPROACH_LISTS)
    assert "לא ניתנה רשימה" not in resolve.requested_block(ok) and "גישת ההכנסות" in resolve.requested_block(ok)


def test_an_ordinal_that_also_fits_the_candidates_or_a_listing_is_told_to_the_model_as_two_readings():
    """Round 10 KTD6, round 15 U6 (KTD1, R2): two substantive readings of an ordinal are the model's to weigh, as a hint in
    the request block; the server asks nothing."""
    r = _resolved(intent=_intent("professional", "prior_answer_item", item=2))
    both = _validate(r, "השנייה", candidates=CANDIDATES, lists=APPROACH_LISTS)
    assert not both.server_clarify and not both.clarify
    assert both.hints and "גישת ההכנסות" in both.hints[0] and "מתחם הגפן ב" in both.hints[0]
    assert both.candidates == [] and both.resolution["decisions"]["item"].startswith("ambiguous")
    assert "גישת ההכנסות" in resolve.requested_block(both)
    listing = [{"document_id": "d-1", "title": "שומה א"}, {"document_id": "d-2", "title": "שומה ב"}]
    listed = _validate(r, "השנייה", lists=APPROACH_LISTS, listing=listing)
    assert not listed.clarify and listed.hints and "שומה ב" in listed.hints[0] and "ברשימת המסמכים" in listed.hints[0]
    same = _validate(r, "השנייה", lists=APPROACH_LISTS, listing=[{"document_id": "d-1", "title": "גישת ההשוואה"},
                                                        {"document_id": "d-2", "title": "גישת ההכנסות"}])
    assert not same.hints  # the listing and the list name the same thing: one reading
    short = _validate(_resolved(intent=_intent("professional", "prior_answer_item", item=3)), "השלישית",
                      candidates=CANDIDATES, lists=APPROACH_LISTS)
    assert not short.hints  # the third fits no candidate: one reading


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
    req = _validate(r, "הסבר את הגישה השנייה", lists=APPROACH_LISTS)
    block = resolve.requested_block(req)
    assert "פריט 2" in block and "«גישת ההכנסות»" in block


def test_an_answer_that_enumerates_in_prose_gives_its_bold_terms_as_items():
    # round 10 real-model finding: the approaches came as one paragraph of bold terms, not a list
    from app.chat import slots

    prose = "שלוש גישות השומה הבסיסיות הן **גישת ההשוואה** [S1], **גישת היוון ההכנסות** [S2] ו**גישת העלות** [S3]."
    assert slots.extract_items(prose) == ["גישת ההשוואה", "גישת היוון ההכנסות", "גישת העלות"]
    leads = "**גישת ההשוואה**: לפי עסקאות.\n\n**גישת העלות**: לפי עלות.\n\nסיכום."
    assert slots.extract_items(leads) == ["גישת ההשוואה", "גישת העלות"]
    assert slots.extract_items("**כותרת**\n\nטקסט רגיל.") == []
    assert slots.extract_items("1. אחת\n2. שתיים\n\n**א** ו**ב**") == ["אחת", "שתיים"]  # a list comes first


def test_one_bold_span_that_enumerates_gives_its_items():
    # round 10 real-model run on 044fda4: "**גישת ההשוואה, גישת ההכנסות וגישת העלות**" in one bold span
    from app.chat import slots

    assert slots.extract_items("הגישות הן **גישת ההשוואה, גישת ההכנסות וגישת העלות** [S1].") == [
        "גישת ההשוואה", "גישת ההכנסות", "גישת העלות"]
    assert slots.extract_items("**שווי שוק** הוא המחיר.") == []


# --- round 22 U12 (KTD14, R20-R22): several named lists, built at compose time, bound by the user's noun -------------------------------

PLOTS_AND_PAYMENTS = """להלן המגרשים בשומה:

1. מגרש 101 — 500 מ"ר [S1]
2. מגרש 102 — 620 מ"ר [S1]
3. מגרש 103 — 480 מ"ר [S1]

**לוח התשלומים:**

- תשלום ראשון — 1,000 ₪ בחתימה [V1]
- תשלום שני — 2,000 ₪ במסירה [V2]
- תשלום שלישי — 3,000 ₪ באכלוס [V3]
"""


def _printed(markdown, mid="m-1", docs_of=None, fallback=()):
    out = slots.carry(None, intent="property", lists=slots.lists_of(markdown, docs_of, fallback), message_id=mid)
    return out["lists"]


def test_both_lists_of_an_answer_are_built_with_title_kind_refs_and_documents():
    docs = {"S1": ["d-plots"], "V1": ["d-pay"], "V2": ["d-pay"], "V3": ["d-pay"]}
    lists = _printed(PLOTS_AND_PAYMENTS, docs_of=lambda t: [d for k, v in docs.items() if f"[{k}]" in t for d in v])
    plots, payments = lists
    assert (plots["title"], plots["kind"], plots["total"], plots["document_ids"]) == ("להלן המגרשים בשומה", "מגרש", 3, ["d-plots"])
    assert [i["label"] for i in plots["items"]] == ["מגרש 101", "מגרש 102", "מגרש 103"]
    assert (payments["title"], payments["kind"], payments["document_ids"]) == ("לוח התשלומים", "תשלום", ["d-pay"])
    assert [i.get("ref") for i in payments["items"]] == ["V1", "V2", "V3"]  # an item that carries a reference keeps it
    assert [x["id"] for x in lists] == ["m-1#1", "m-1#2"]


def test_a_table_is_a_list_of_its_rows_named_by_its_first_column():
    md = ("הנה הטבלה:\n\n| מגרש | שטח |\n|---|---|\n| 101 | 500 |\n| 102 | 620 |\n| סה\"כ | 1,120 |\n\nסוף.")
    (entry,) = _printed(md)
    assert [i["label"] for i in entry["items"]] == ["מגרש 101", "מגרש 102"]  # a bare number is named by the header; a total is no item
    assert entry["title"] == "הנה הטבלה" and entry["kind"] == "מגרש"
    assert _printed("| א | ב |\n|---|---|\n| 1 | 2 |") == []  # one row is no list


def test_a_list_keeps_every_item_it_printed_up_to_the_cap_and_its_total():
    md = "\n".join(f"{i}. מגרש {100 + i}" for i in range(1, 16))
    (entry,) = _printed(md)
    assert len(entry["items"]) == entry["total"] == 15
    huge = "\n".join(f"{i}. מגרש {100 + i}" for i in range(1, 61))
    (entry,) = _printed(huge)
    assert len(entry["items"]) == slots.MAX_LIST_ITEMS and entry["total"] == 60


def test_an_answer_without_a_list_or_a_table_has_none_and_prose_bold_terms_are_one_list():
    assert _printed("אין כאן רשימה.") == []
    (entry,) = _printed("הגישות הן **גישת ההשוואה** [S1], **גישת ההכנסות** [S2] ו**גישת העלות** [S3].")
    assert [i["label"] for i in entry["items"]] == ITEMS


def two_lists():
    return _printed(PLOTS_AND_PAYMENTS)


def test_ae6_the_second_payment_binds_to_the_payments_list_and_not_to_plot_two():
    got = slots.bind("ומה לגבי התשלום השני?", two_lists(), item=2)
    assert got.kind == "item" and got.items == (2,) and got.labels == ("תשלום שני",) and got.list_id == "m-1#2"
    plot = slots.bind("ומה לגבי המגרש השני?", two_lists(), item=2)
    assert plot.labels == ("מגרש 102",) and plot.list_id == "m-1#1"
    plural = slots.bind("ומה לגבי השלישי מבין התשלומים?", two_lists(), item=3)
    assert plural.labels == ("תשלום שלישי",)  # the noun may stand after the ordinal; plural and singular are one noun


def test_a_bare_ordinal_with_two_plausible_lists_asks_one_question_that_names_both():
    got = slots.bind("ומה לגבי השני?", two_lists(), item=2)
    assert got.kind == "ask" and not got.bound
    assert "להלן המגרשים בשומה" in got.ask and "לוח התשלומים" in got.ask and got.ask.count("?") == 1


def test_the_model_cannot_choose_the_list_the_user_did_not_name_and_the_server_does_not_trust_its_list_id():
    # the model names the payments list for a bare «השני» over two lists of one answer: still a question
    assert slots.bind("ומה לגבי השני?", two_lists(), list_id="m-1#2", item=2).kind == "ask"
    # the model names the plots list although the user's noun is the payments: the selected list wins
    got = slots.bind("ומה לגבי התשלום השני?", two_lists(), list_id="m-1#1", item=2)
    assert got.list_id == "m-1#2" and got.labels == ("תשלום שני",)


def test_the_only_list_in_which_the_place_exists_is_the_one_bound():
    lists = [a_list(["א", "ב"], "קטנה", mid="m-1", n=1), a_list(["תשלום א", "תשלום ב", "תשלום ג", "תשלום ד"], "ארבעה", mid="m-1", n=2)]
    got = slots.bind("ומה לגבי הרביעי?", lists, item=4)
    assert got.list_id == "m-1#2" and got.labels == ("תשלום ד",)


def test_with_lists_from_different_answers_the_most_recent_answers_list_wins_and_one_of_the_same_answer_asks():
    older = a_list(["מגרש 1", "מגרש 2", "מגרש 3"], "מגרשים", mid="m-1", age=1)
    newer = a_list(["תשלום 1", "תשלום 2", "תשלום 3"], "תשלומים", mid="m-2")
    assert slots.bind("ומה לגבי השני?", [newer, older], item=2).labels == ("תשלום 2",)
    assert slots.bind("ומה לגבי המגרש השני?", [newer, older], item=2).labels == ("מגרש 2",)  # the noun beats recency
    sibling = a_list(["יח 1", "יח 2"], "יחידות", mid="m-2", n=2)
    ask = slots.bind("ומה לגבי השני?", [newer, sibling, older], item=2)
    # the question names every list the place fits, the older answer's too, and says which one is older
    assert ask.kind == "ask" and "תשלומים" in ask.ask and "יחידות" in ask.ask and "«מגרשים» (3 פריטים - מתשובה קודמת)" in ask.ask


def test_the_twelfth_binds_in_a_list_of_fifteen_in_either_hyphen_form_and_the_last_is_the_fifteenth():
    plots = a_list([f"מגרש {100 + i}" for i in range(1, 16)], "המגרשים")
    for message in ("ומה לגבי המגרש ה-12?", "מה השטח של ה-12?", "ומה לגבי מגרש 12?"):
        got = slots.bind(message, [plots], item=12)
        assert got.kind == "item" and got.labels == ("מגרש 112",), message
    assert slots.bind("ומה לגבי האחרון?", [plots], item=15).labels == ("מגרש 115",)
    assert slots.bind("ומה לגבי המגרש ה-12?", [plots], item=5).reason.startswith("item 5 contradicts")


def test_the_twelfth_of_a_list_of_sixty_that_was_not_stored_is_refused_with_its_reason_not_bound_to_a_wrong_item():
    big = a_list([f"מגרש {i}" for i in range(1, 61)], "המגרשים", total=70)
    assert slots.bind("ומה לגבי המגרש ה-65?", [big], item=65).reason.endswith("only 40 were stored")


@pytest.mark.parametrize("message", ["מה היה השווי ב-2012?", "מה השטח בגוש 6012?", "מה התשואה, 12%?", "מה היה ב-12/5?", "מה השטח בקומה 12?"])
def test_a_number_that_is_no_place_binds_to_no_item_even_when_a_long_list_is_in_scope(message):
    plots = a_list([f"מגרש {100 + i}" for i in range(1, 16)], "המגרשים")
    got = slots.bind(message, [plots], item=12)
    assert got.kind == "none" and "no ordinal" in got.reason, message


def test_both_binds_only_to_a_list_of_two_items_and_asks_over_three():
    pair = a_list(["בניין א", "בניין ב"], "הבניינים")
    got = slots.bind("ומה לגבי שניהם?", [pair], both=True, item_set=[1, 2])
    assert got.kind == "set" and got.items == (1, 2) and got.labels == ("בניין א", "בניין ב")
    assert slots.bind("ומה לגבי שתיהן?", [pair]).kind == "set"
    three = a_list(["בניין א", "בניין ב", "בניין ג"], "הבניינים")
    ask = slots.bind("ומה לגבי שניהם?", [three], both=True, item_set=[1, 2])
    assert ask.kind == "ask" and "3 פריטים" in ask.ask and not ask.bound  # never "all"
    assert slots.bind("ומה לגבי שניהם?", two_lists(), both=True).kind == "ask"  # two lists, none of two items: a question


def test_both_with_several_lists_binds_to_the_one_list_the_noun_names_and_to_nothing_otherwise():
    pair = a_list(["חוזה א", "חוזה ב"], "החוזים", n=2)
    plots = a_list(["מגרש 1", "מגרש 2", "מגרש 3"], "המגרשים", n=1)
    assert slots.bind("ומה לגבי שני החוזים — שניהם?", [plots, pair], both=True).kind == "set"
    assert slots.bind("ומה לגבי שניהם?", [plots, pair], both=True).kind == "ask"  # two lists in the latest answer: ask, never guess


def test_an_item_set_without_both_in_the_message_is_refused_and_both_in_the_message_needs_no_ordinal():
    pair = a_list(["בניין א", "בניין ב"], "הבניינים")
    got = slots.bind("ומה לגבי הראשון והשני?", [pair], both=True, item_set=[1, 2])
    assert got.kind == "none" and "שניהם" in got.reason


def test_no_lists_no_binding_and_an_ordinal_word_is_flagged_as_no_list():
    got = slots.bind("הסבר את השנייה", [], item=2)
    assert got.kind == "none" and got.no_list
    assert not slots.bind("מה השווי ב-2024?", [], item=2).no_list


def test_settle_records_the_selected_list_the_set_and_the_question():
    both = settle(IntentClaim(intent="property", reference="prior_answer_item", item=None, topic="", standard_refs=[],
                              item_scope="both", item_set=[1, 2]), "ומה לגבי שניהם?", VOCABULARY,
                  lists=[a_list(["בניין א", "בניין ב"], "הבניינים")])
    assert both.bound == "item_set" and both.item_set == (1, 2) and both.item_labels == ("בניין א", "בניין ב") and both.item is None
    pay = settle(IntentClaim(intent="property", reference="prior_answer_item", item=2, topic="", standard_refs=[],
                             list_id="m-1#2"), "ומה לגבי התשלום השני?", VOCABULARY, lists=two_lists())
    assert pay.bound == "item" and pay.list_id == "m-1#2" and pay.item_label == "תשלום שני" and pay.list_title == "לוח התשלומים"
    asked = settle(IntentClaim(intent="property", reference="prior_answer_item", item=2, topic="", standard_refs=[]),
                   "ומה לגבי השני?", VOCABULARY, lists=two_lists())
    assert asked.bound == "" and asked.ask and asked.reference == "none" and not asked.no_list
    assert asked.as_dict()["ask"] == asked.ask


def test_the_claim_schema_carries_the_list_and_the_item_set_and_older_claims_still_validate():
    schema = IntentClaim.model_json_schema()
    assert {"list_id", "item_set", "item_scope"} <= set(schema["properties"])
    assert IntentClaim(intent="property", reference="none", item=None, topic="", standard_refs=[]).item_scope == "item"
    with pytest.raises(ValueError):
        IntentClaim(intent="property", reference="none", item=None, topic="", standard_refs=[], item_scope="all")
    assert resolve.SCHEMA_VERSION == 2


def test_validate_asks_the_server_question_for_a_bare_ordinal_over_two_lists_and_binds_the_noun():
    r = _resolved(standalone_question="ומה לגבי השני?", intent=_intent("property", "prior_answer_item", item=2))
    req = _validate(r, "ומה לגבי השני?", lists=two_lists())
    assert req.server_clarify and req.clarify and "לוח התשלומים" in req.clarify
    assert req.resolution["decisions"]["item"].startswith("ask") and req.resolution["schema_version"] == 2
    named = _resolved(standalone_question="ומה לגבי התשלום השני?", intent=_intent("property", "prior_answer_item", item=2))
    req = _validate(named, "ומה לגבי התשלום השני?", lists=two_lists())
    assert not req.clarify and req.intent.item_label == "תשלום שני" and req.topic == "תשלום שני"
    assert "תשלום שני" in req.standalone_question and req.resolution["decisions"]["item"].startswith("bound: 2")


def test_validate_names_both_items_in_the_standalone_question_and_the_requested_block():
    pair = [a_list(["בניין א", "בניין ב"], "הבניינים")]
    r = _resolved(standalone_question="מה השווי של שניהם?", intent=dict(_intent("property", "prior_answer_item"), item_scope="both",
                                                                       item_set=[1, 2]))
    req = _validate(r, "מה השווי של שניהם?", lists=pair)
    assert req.intent.bound == "item_set" and req.topic == "בניין א ובניין ב"
    assert "בניין א" in req.standalone_question and "בניין ב" in req.standalone_question
    assert "שני הפריטים ברשימה «הבניינים»" in resolve.requested_block(req)


def test_the_resolve_call_is_shown_every_list_with_its_id_title_kind_and_items_and_the_date_field():
    text = resolve._input({"metric_kind": "value", "date_field": "valuation", "document_ids": ["d-1"]}, [], "ומה לגבי השני?", [],
                          lists=two_lists() + [a_list(["א", "ב"], "ישנה", mid="m-0", age=2)])
    assert "רשימה m-1#1 «להלן המגרשים בשומה» (סוג: מגרש, 3 פריטים):" in text and "2. תשלום שני" in text
    assert "רשימה m-1#2 «לוח התשלומים»" in text and "מתשובה קודמת" in text and "date_field" in text and "valuation" in text


# --- the lists are gated one by one by their own documents (R22, N19) -----------------------------------------------------------------


def test_hiding_one_lists_document_drops_only_that_list_keeps_its_id_as_a_tombstone_and_renumbers_nothing():
    plots = a_list(["מגרש 1", "מגרש 2"], "מגרשים", mid="m-1", docs=["d-plots"])
    pay = a_list(["תשלום 1", "תשלום 2", "תשלום 3"], "תשלומים", mid="m-1", n=2, docs=["d-pay"])
    ctx = slots.carry(None, intent="property", message_id="m-2") | {"lists": [plots, pay]}
    kept, dropped = slots.gate_lists(ctx, {"d-pay"})
    assert dropped == ["m-1#1"] and [x["id"] for x in kept["lists"]] == ["m-1#2"] and kept["tombstones"] == ["m-1#1"]
    # the payments list is still usable and keeps its id; "השני" no longer has two readings
    assert slots.bind("ומה לגבי השני?", kept["lists"], item=2).labels == ("תשלום 2",)
    again, none = slots.gate_lists(kept, {"d-pay"})
    assert none == [] and again is kept
    assert slots.gate_lists(ctx, {"d-pay", "d-plots"})[1] == []
    assert slots.list_documents(ctx) == {"d-plots", "d-pay"}


def test_a_list_that_mixes_a_hidden_document_is_dropped_whole():
    mixed = a_list(["א", "ב"], "מעורבת", docs=["d-1", "d-2"])
    kept, dropped = slots.gate_lists({"lists": [mixed]}, {"d-1"})
    assert kept["lists"] == [] and dropped == [mixed["id"]]


# --- an answer stored before it had lists is read by the legacy shim only -------------------------------------------------------------


def test_the_legacy_reader_gives_the_one_list_a_stored_answer_printed_and_nothing_for_an_answer_without_a_list():
    (entry,) = slots.legacy_lists(PLOTS_AND_PAYMENTS, ["d-1"])
    assert [i["label"] for i in entry["items"]] == ["מגרש 101", "מגרש 102", "מגרש 103"]  # numbered first: the old extractor's rule
    assert entry["document_ids"] == ["d-1"] and entry["total"] == 3
    assert slots.legacy_lists("אין כאן רשימה") == []


# --- the date field of the datum ("ומה לגבי המועד") ---------------------------------------------------------------------------------------


def test_the_date_field_of_the_datum_is_a_carried_parameter_that_survives_a_professional_turn():
    from app.chat import api

    assert api._date_field({"time": {"timeline": [{"date_kind": None}, {"date_kind": "valuation"}]}}) == "valuation"
    assert api._date_field({"time": {"timeline": []}}) == "" and api._date_field({}) == ""
    first = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS | {"date_field": "valuation"}, message_id="m-1")
    prof = slots.carry(first, intent="professional", topic="גישות", message_id="m-2")
    assert slots.join_focus(prof)["date_field"] == "valuation"  # the same datum's date, dormant with the property
    assert "date_field" not in slots.join_focus(slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, message_id="m-1"))
    prop, params = slots.split_focus(FOCUS_WITH_DATE)
    assert params["date_field"] == "transaction" and "date_field" not in prop


FOCUS_WITH_DATE = {"metric_kind": "value", "unit": "ILS", "document_ids": ["d-1"], "subject": "הנכס", "date_field": "transaction"}


# --- a follow-up that changed only the metric keeps the property (R21) ------------------------------------------------------------------


def _outcome(intent: str = "property", status: str = "answered", period=None):
    from types import SimpleNamespace

    from app.chat import timeframe

    ws = SimpleNamespace(intent=intent, user_messages=[{"text": "ומה לגבי השווי?", "current": True}], time=period or timeframe.TimeMode())
    return SimpleNamespace(workspace=ws, answer=SimpleNamespace(status=status), resolution={}, server_asked=False, lists=[])


def _payload(focus, entity_changed=False):
    return {"request": {"entity_changed": entity_changed, "document_ids": []}, "focus": focus, "unit": None, "sources": [],
            "pending": None, "time": {"timeline": []}}


def test_an_answer_that_declares_the_new_metric_without_repeating_the_documents_keeps_the_property():
    from app.chat import api

    previous = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, message_id="m-1")
    bare = {"metric_as_written": "שווי", "metric_kind": "value", "unit": "ILS", "period": "unknown", "area_basis": "", "vat": "unknown",
            "subject": "", "value_role": "appraiser_determination", "document_ids": []}
    out = api._context(_outcome(), _payload(bare), previous, "m-2")
    assert out["property"] == PROPERTY  # the documents, the unit and the subject of the property it was in
    assert out["params"]["metric_kind"] == "value" and out["params"]["metric_as_written"] == "שווי"  # only the metric parameters changed
    moved = api._context(_outcome(), _payload(bare, entity_changed=True), previous, "m-2")
    assert moved["property"]["document_ids"] == []  # the user changed the property: nothing is kept
    named = api._context(_outcome(), _payload(bare | {"document_ids": ["d-9"], "subject": "אחר"}), previous, "m-2")
    assert named["property"]["document_ids"] == ["d-9"]


def test_the_period_a_turn_ran_under_is_stored_with_its_property_and_kept_when_the_turn_found_no_datum():
    from app.chat import api, timeframe

    time = timeframe.carried(timeframe.resolve("ומה לגבי השוכר?", None), {"requested": {"kind": "point", "start": "2022", "end": "2022",
                                                                                      "precision": "year", "text": "2022"},
                                                                         "source_message_id": "m-1"})
    previous = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, message_id="m-1")
    focus = {"metric_kind": "value", "unit": "ILS", "document_ids": ["d-1"], "subject": "מתחם הדולב"}
    out = api._context(_outcome(period=time), _payload(focus), previous, "m-2")
    assert out["period"]["source_message_id"] == "m-1" and out["period"]["requested"]["start"] == "2022"
    nothing = slots.carry(None, intent="property", property=PROPERTY, params=PARAMS, period=out["period"], message_id="m-2")
    again = api._context(_outcome(), _payload(None), nothing, "m-3")  # no datum found, nowhere moved: the property and its period stay
    assert again["property"] == PROPERTY and again["period"] == out["period"]
