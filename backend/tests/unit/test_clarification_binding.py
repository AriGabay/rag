"""How a reply is bound to the clarification it answers, and how the clarification's state is read (round 26 U3; KTD3, KTD5; R9-R11).

Pure functions over the stored record (``{id, kind, options[{id, label, detail, ref}], allow_all, ...}``): the deterministic binder runs
before any model call, so a failed relation call still binds (cause 3 of the repeated list) and a reply that fits several options narrows
to exactly those (cause 4). Invented names and phrasings only."""

from __future__ import annotations

import pytest

from app.chat import clarify


def option(n: int, label: str, detail: str = "") -> dict:
    return {"id": f"opt{n}", "label": label, "detail": detail, "ref": {"document_id": f"d{n}"}}


UNITS = {"id": "c1", "kind": "unit", "allow_all": False, "options": [
    option(1, "שומה - הרימון 14 שכונת הגבעה", "15/03/2024"),
    option(2, "שומה - הרימון 14 שכונת הנחל", "02/09/2022"),
    option(3, "שומה - הרימון 14 שכונת הנחל הצפוני", "01/01/2021")]}
PERIODS = {"id": "c2", "kind": "period", "allow_all": True, "options": [option(1, "מרץ 2022", "מרץ 2022"), option(2, "ספטמבר 2023", "ספטמבר 2023")]}


def ids(match: clarify.Match) -> list[str]:
    return [o["id"] for o in match.options]


@pytest.mark.parametrize("reply, expected", [
    ("2", ["opt2"]), ("מספר 2", ["opt2"]), ("אפשרות 3", ["opt3"]), ("השנייה", ["opt2"]), ("את השלישית", ["opt3"]),
    ("שכונת הגבעה", ["opt1"]), ("הגבעה", ["opt1"]), ("התכוונתי לשכונת הגבעה", ["opt1"]), ("הצפוני", ["opt3"]),
])
def test_a_reply_that_names_one_option_binds_it(reply, expected):
    m = clarify.match_reply(UNITS, reply)
    assert m.kind == "one" and ids(m) == expected


def test_a_reply_that_fits_several_options_narrows_to_exactly_those():
    m = clarify.match_reply(UNITS, "הנחל")
    assert m.kind == "several" and ids(m) == ["opt2", "opt3"]  # never the whole list again


@pytest.mark.parametrize("reply", [
    "2 חדרים", "בשנת 2", "החמישית", "0", "9", "מה שטח המגרש בשכונת הגבעה?", "לא, התכוונתי לשכונת הגבעה", "הרימון", "שכונת"])
def test_a_reply_that_is_no_choice_binds_nothing(reply):
    assert clarify.match_reply(UNITS, reply).kind == "none", reply


def test_a_number_binds_as_a_place_only_when_it_is_the_whole_message_and_a_year_binds_by_its_text():
    assert ids(clarify.match_reply(PERIODS, "2")) == ["opt2"]
    assert ids(clarify.match_reply(PERIODS, "2023")) == ["opt2"]  # a year is text of an option, not a place
    assert clarify.match_reply(PERIODS, "2 חדרים").kind == "none"
    assert clarify.match_reply(PERIODS, "2024").kind == "none"


def test_all_binds_only_when_the_clarification_offered_it():
    assert clarify.match_reply(PERIODS, "כולן").kind == "all" and clarify.match_reply(PERIODS, "שניהם").kind == "all"
    assert clarify.match_reply(UNITS, "כולן").kind == "none"


@pytest.mark.parametrize("reply, kind", [
    ("לא יודע", "help"), ("לא יודעת.", "help"), ("תסביר", "help"), ("אין לי מושג", "help"),
    ("ביטול", "cancel"), ("עזוב את זה", "cancel"), ("עזוב", "cancel"), ("לא יודע מה שטח המגרש", "none")])
def test_help_and_cancel_are_whole_message_requests(reply, kind):
    assert clarify.match_reply(UNITS, reply).kind == kind


def test_a_clarification_with_no_options_binds_nothing():
    assert clarify.match_reply({"id": "c", "kind": "text", "options": [], "allow_all": False}, "2").kind == "none"


# --- the clarification's state, one function for every reader (KTD3, KTD5) ---------------------------------------------------


def test_a_clarification_nobody_replied_to_is_open_only_while_it_is_the_latest_message():
    assert clarify.state_of("c1", next_user=False, reply=None) == {"state": "open", "chosen": None}
    assert clarify.state_of("c1", next_user=False, reply=None, current=False)["state"] == "stale"


def test_a_recorded_choice_answers_it_and_marks_the_chosen_option_even_when_the_continuation_failed():
    reply = {"clarification_id": "c1", "option_id": "opt2", "outcome": "answered"}
    assert clarify.state_of("c1", next_user=True, reply=reply, current=False) == {"state": "answered", "chosen": "opt2"}


@pytest.mark.parametrize("outcome", ["help", "cancelled", "abandoned", "narrowed"])
def test_a_reply_that_did_not_choose_closes_it(outcome):
    reply = {"clarification_id": "c1", "option_id": None, "outcome": outcome}
    assert clarify.state_of("c1", next_user=True, reply=reply, current=False)["state"] == "closed"


def test_a_later_message_with_no_recorded_outcome_makes_it_stale_unless_its_turn_is_still_running():
    assert clarify.state_of("c1", next_user=True, reply=None, current=False)["state"] == "stale"
    assert clarify.state_of("c1", next_user=True, reply=None, in_flight=True, current=False)["state"] == "closed"
    other = {"clarification_id": "zzz", "option_id": "opt1", "outcome": "answered"}
    assert clarify.state_of("c1", next_user=True, reply=other, current=False)["state"] == "stale"


def test_the_public_form_hides_the_references_and_a_clarification_without_options_has_none():
    record = {**UNITS, "question": "מי?", "reason": "כמה שומות"}
    public = clarify.public(record)
    assert public == {"id": "c1", "question": "מי?", "reason": "כמה שומות", "kind": "unit", "allow_all": False, "options": [
        {"id": o["id"], "label": o["label"], "detail": o["detail"]} for o in UNITS["options"]]}
    assert clarify.public({**record, "options": []}) is None


def test_a_help_reply_reemits_the_options_under_a_new_id_with_the_help_counted():
    again = clarify.reemit({**UNITS, "question": "מי?", "reason": "", "help_count": 0})
    assert again["id"] != "c1" and [o["id"] for o in again["options"]] == ["opt1", "opt2", "opt3"] and again["help_count"] == 1


# --- the options the older candidate binder picks (``engine._picked_options``) ------------------------------------------------------------


def _request(**kw):
    from app.chat import resolve

    return resolve.Request("clarification_answer", "?", **kw)


def _period_option(n: int, start: str, end: str) -> dict:
    return {"id": f"per{n}", "label": f"תקופה {n}", "detail": "", "ref": {"start": start, "end": end, "precision": "day"}}


def _unit_option(n: int, document_id: str) -> dict:
    return {"id": f"unit{n}", "label": f"נכס {n}", "detail": "", "ref": {"document_id": document_id, "title": f"נכס {n}"}}


@pytest.mark.parametrize("offered", [
    [{"document_id": "d9", "title": "שומה אחרת"}],
    [{"document_id": "d1", "title": "שומה"}, {"document_id": "d2", "title": "שומה נוספת"}],
])
def test_a_reply_among_offered_candidates_picks_nothing_of_a_clarification_whose_options_are_not_units(offered):
    from app.chat import engine

    options = [_period_option(1, "2022-03-01", "2022-03-31"), _period_option(2, "2023-09-01", "2023-09-30")]
    assert engine._picked_options(options, _request(offered=offered)) == []  # a period option never matches a candidate; no KeyError
    phrases = [{"id": "ph1", "label": "שטח עיקרי", "detail": "", "ref": {"phrase": "שטח עיקרי"}}]
    assert engine._picked_options(phrases, _request(offered=offered)) == []


def test_a_reply_among_offered_candidates_still_picks_the_unit_options_it_fits():
    from app.chat import engine

    options = [_unit_option(1, "d1"), _unit_option(2, "d2"), _unit_option(3, "d3")]
    offered = [{"document_id": "d3", "title": "x"}, {"document_id": "d1", "title": "y"}]
    assert engine._picked_options(options, _request(offered=offered)) == ["unit1", "unit3"]
    assert engine._picked_options(options, _request()) == []  # nothing offered: nothing picked


# --- the continuation starts from the intent and scope the clarifying turn settled (review #8) ------------------------------------------------


def _continuing(request: dict | None):
    from app.chat import engine

    record = UNITS | {"original_question": "מה שטח המגרש בהרימון 14?", "request": clarify.request_summary(request)}
    return engine._continuation_request(clarify.Continuation(record, clarify.CHOICE, [UNITS["options"][0]]), None)


@pytest.mark.parametrize("intent", ["professional", "property", "mixed", "document", "set", "unknown"])
def test_a_continuation_keeps_the_intent_the_clarifying_turn_settled(intent):
    assert _continuing({"intent": {"value": intent}}).intent.value == intent


@pytest.mark.parametrize("request_", [None, {}, {"intent": {}}, {"intent": {"value": "banana"}}, {"intent": {"value": ""}}])
def test_a_record_with_no_valid_intent_continues_as_a_property_question(request_):
    assert _continuing(request_).intent.value == "property"


@pytest.mark.parametrize("scope, expected", [("set", "set"), ("entity", "entity"), (None, "entity"), ("galaxy", "entity")])
def test_a_continuation_carries_the_stored_scope(scope, expected):
    assert _continuing({"scope": scope, "intent": {"value": "set" if scope == "set" else "property"}}).scope == expected
