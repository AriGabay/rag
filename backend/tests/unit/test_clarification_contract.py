"""The clarification contract and its server-side checks, without a database (round 26 U2; KTD1, KTD2; R4-R6, R10).

The structured clarification inside the final answer, the stable option ids, the period and phrase grounding against
the sources a turn read, the value guard, the repeat and cap rules, and the display of units that have no title. The
model is not called; every text is invented."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.chat import clarify, contexts, engine, entities
from tests.support.scripted_agent import final, offer


def source(text: str = "", date_line: str | None = None, title: str = "שומה - הדגמה", location: str = 'סעיף "סיכום"',
           listing: bool = False, server_made: bool = False):
    return SimpleNamespace(is_listing=listing, server_made=server_made, text=text, date_line=date_line, title=title,
                           location=location, document_id="d1")


def workspace(*sources, coverage=None):
    return SimpleNamespace(sources={f"S{n}": s for n, s in enumerate(sources, 1)}, time_coverage=coverage)


# --- the schema (KTD1) --------------------------------------------------------------------------------------------------


def test_the_final_schema_carries_the_clarify_object_with_four_kinds_and_at_most_four_options():
    schema = engine.FINAL_SCHEMA
    assert "clarify" in schema["required"] and "default" not in schema["properties"]["clarify"]
    node = schema["$defs"]["ClarifyOffer"]
    assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"]) == {
        "question", "reason", "kind", "options", "allow_all"}
    assert node["properties"]["kind"]["enum"] == ["unit", "period", "area_basis", "method"]  # no kind to choose a value
    assert node["properties"]["options"]["maxItems"] == clarify.MAX_OPTIONS == 4
    option = schema["$defs"]["OfferedOption"]
    assert set(option["required"]) == set(option["properties"]) == {"key", "label", "ref"}


def test_clarify_is_listed_after_the_question_and_a_final_answer_without_it_still_validates():
    props = list(engine.FINAL_SCHEMA["properties"])
    assert props.index("clarify") == props.index("clarification_question") + 1
    body = final("תשובה")["final"]
    body.pop("clarify")
    assert engine.FinalAnswer.model_validate(body).clarify is None  # code that builds an answer says nothing of it


def test_a_clarify_object_validates_with_its_options_and_refuses_an_unknown_kind_or_field():
    body = final("שאלה", "clarification", clarification="שאלה", clarify=offer("period", "שאלה", ("2023", "2023"), ("2024", "2024")))["final"]
    answer = engine.FinalAnswer.model_validate(body)
    assert [(o.key, o.label, o.ref) for o in answer.clarify.options] == [("1", "2023", "2023"), ("2", "2024", "2024")]
    for kind in ("value", "metric", "other"):
        with pytest.raises(ValidationError):
            engine.FinalAnswer.model_validate({**body, "clarify": {**body["clarify"], "kind": kind}})
    with pytest.raises(ValidationError):
        engine.FinalAnswer.model_validate({**body, "clarify": {**body["clarify"], "extra": 1}})


# --- option ids -------------------------------------------------------------------------------------------------------


def test_an_option_id_is_a_stable_hash_of_the_kind_and_the_reference():
    ref = {"document_id": "d1", "title": "x"}
    assert clarify.option_id("unit", ref) == clarify.option_id("unit", {"title": "x", "document_id": "d1"})
    assert clarify.option_id("unit", ref) != clarify.option_id("period", ref)
    assert clarify.option_id("period", {"start": "2023", "end": "2023", "precision": "year"}) != clarify.option_id(
        "period", {"start": "2024", "end": "2024", "precision": "year"})
    assert len(clarify.option_id("unit", ref)) == 12


def test_a_units_id_follows_the_unit_not_the_text_around_it():
    anchor = {"document_id": "d1", "reading_id": "r1", "unit_no": 2, "units_tag": None}
    one = {"document_id": "d1", "title": "קובץ", "unit": {**anchor, "display": None, "pages": [2, 2]}, "file_title": "קובץ"}
    two = {"document_id": "d1", "title": "אחר", "unit": {**anchor, "display": "כותרת", "pages": [2, 3]}, "file_title": "קובץ ב"}
    assert clarify.ref_identity(one) == clarify.ref_identity(two) == anchor
    assert clarify.ref_identity({"document_id": "d1", "title": "t"}) == {"document_id": "d1"}


# --- periods (KTD2) -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("ref, expected", [
    ("2024", ("2024", "2024", "year")), ("2024-03", ("2024-03", "2024-03", "month")),
    ("2024-03-15", ("2024-03-15", "2024-03-15", "day")), ("2022/2024", ("2022", "2024", "year")),
    ("2022-01-01..2022-12-31", ("2022-01-01", "2022-12-31", "day")), (" 2023-05 / 2023-06 ", ("2023-05", "2023-06", "month")),
])
def test_a_period_reference_is_an_iso_point_or_range(ref, expected):
    period = clarify.parse_period(ref)
    assert (period.start, period.end, period.precision) == expected


@pytest.mark.parametrize("ref", ["", "אתמול", "2024-13", "2024-02-30", "2024/2024-05", "2025/2024", "1850", "2024-03-15/", "0000"])
def test_a_reference_that_is_no_iso_period_is_none(ref):
    assert clarify.parse_period(ref) is None


def test_a_period_is_grounded_when_a_stated_date_lies_inside_it_and_never_by_a_finer_date_than_the_source_gave():
    stated = [("2024-03-15", "2024-03-15"), ("2022-09", "2022-09")]
    assert clarify.period_in_sources(clarify.parse_period("2024-03-15"), stated)
    assert clarify.period_in_sources(clarify.parse_period("2024"), stated)  # a year holds the day
    assert clarify.period_in_sources(clarify.parse_period("2022-01/2022-12"), stated)
    assert not clarify.period_in_sources(clarify.parse_period("2022-09-02"), stated)  # the source says only the month
    assert not clarify.period_in_sources(clarify.parse_period("2019"), stated)
    assert not clarify.period_in_sources(clarify.parse_period("2024-04"), stated)


def test_the_dates_of_the_read_sources_come_from_their_text_their_date_line_and_the_time_coverage():
    ws = workspace(source("המועד הקובע לשומה: 15/03/2024. שטח 612 מ״ר."), source("ללא תאריך", date_line="למועד הקובע 02/09/2022"),
                   source("2019-01-01", listing=True), source("01/01/2001", server_made=True),
                   coverage={"units": [{"date": "2021-05-10"}, {"date": None}]})
    found = set(clarify.source_dates(ws))
    assert {("2024-03-15", "2024-03-15"), ("2022-09-02", "2022-09-02"), ("2021-05-10", "2021-05-10")} <= found
    assert not any("2019" in a or "2001" in a for a, _ in found)  # a listing and the server's own sources are no read source


def test_a_period_becomes_a_requested_time_for_the_continuation():
    point = clarify.requested_of({"start": "2024-03-15", "end": "2024-03-15", "precision": "day"}, "מרץ")
    span = clarify.requested_of({"start": "2022", "end": "2023", "precision": "year"}, "")
    assert (point.kind, point.start, point.end, point.precision, point.text) == ("point", "2024-03-15", "2024-03-15", "day", "מרץ")
    assert (span.kind, span.start, span.end) == ("range", "2022", "2023")


# --- phrases (KTD2) -------------------------------------------------------------------------------------------------------


def test_a_phrase_is_found_in_a_read_source_whatever_the_quotes_and_spacing():
    ws = workspace(source('השטח נמדד כ"שטח   עיקרי" וגם כשטח אקוו׳.'), source("מילה", listing=True))
    assert clarify.find_phrase("שטח עיקרי", ws) is ws.sources["S1"]
    assert clarify.find_phrase('שטח אקוו"', ws) is ws.sources["S1"]
    assert clarify.find_phrase("שטח נטו", ws) is None
    assert clarify.find_phrase("ש", ws) is None  # one letter names nothing
    assert clarify.find_phrase("שטח עיקרי", workspace(source("הערכה לפי בשטח עיקרי בלבד"))) is not None  # a prefix letter
    assert clarify.find_phrase("שטח עיקרי", workspace(source("הערכה לפי שטח עיקרית"))) is None  # not a longer word
    assert clarify.find_phrase("מילה", ws) is None  # a listing is no read source


# --- values are never options (R4) --------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind, label", [
    ("unit", "12,500"), ("unit", "13,100 ₪ למ״ר"), ("area_basis", "612"), ("area_basis", "מ״ר 120"), ("method", "5%"),
    ("method", "1,250,000 ש״ח"), ("period", "12,500 ₪"), ("period", "320 מ״ר"), ("unit", " "), ("area_basis", "אקו׳ 95 מ״ר"),
])
def test_an_option_that_is_only_a_number_or_a_value_is_refused(kind, label):
    assert clarify.label_problem(kind, label) == "value_option"


@pytest.mark.parametrize("kind, label", [
    ("unit", "הרימון 14 שכונת הגבעה"), ("area_basis", "שטח נטו"), ("method", "ממוצע משוקלל"), ("period", "2024"),
    ("period", "15/03/2024"), ("period", "מרץ 2024"), ("unit", "רחוב הצאלון 12"),
])
def test_a_name_a_phrase_or_a_date_is_a_fine_option(kind, label):
    assert clarify.label_problem(kind, label) is None


# --- how an option reads: grounded details, never "untitled" (R6) ---------------------------------------------------


def candidate(**kw):
    details = {"title": None, "name": None, "address": None, "city": None, "parcel": None, "type": None, "date": None,
               "pages": "עמוד 2", "file_title": "קובץ סינתטי"} | kw.pop("details", {})
    unit = {"document_id": "d1", "reading_id": "r", "unit_no": 2, "units_tag": None, "display": kw.pop("title", None),
            "pages": [2, 2], "identifiers": []}
    return {"document_id": "d1", "title": "קובץ סינתטי", "unit": unit, "file_title": "קובץ סינתטי", "details": details}


def test_an_untitled_unit_is_labelled_by_its_card_address_and_told_by_date_and_pages():
    label, detail = clarify.option_texts(candidate(details={"address": "רחוב הצאלון 8", "date": "01/02/2023"}))
    assert label == "רחוב הצאלון 8" and "01/02/2023" in detail and "עמוד 2" in detail and "קובץ סינתטי" in detail
    assert contexts.MSG_NO_TITLE not in label + detail


def test_a_titled_unit_keeps_its_title_and_gains_the_card_details():
    label, detail = clarify.option_texts(candidate(title="מתחם הגפן א", details={"address": "רחוב הגפן 3", "city": "עיר-דוגמה"}))
    assert label == "מתחם הגפן א" and "רחוב הגפן 3" in detail and "עיר-דוגמה" in detail


def test_a_unit_with_nothing_but_pages_is_still_distinguished_by_them_and_a_unit_with_nothing_at_all_is_none():
    label, detail = clarify.option_texts(candidate())
    assert "עמוד 2" in label + detail and contexts.MSG_NO_TITLE not in label + detail
    assert clarify.option_texts(candidate(details={"pages": "", "file_title": ""})) is None


def test_a_document_is_labelled_by_its_title_and_its_valuation_date():
    doc = {"document_id": "d2", "title": "שומה - רחוב הדקל 5", "details": {"title": "שומה - רחוב הדקל 5", "date": "03/04/2021"}}
    assert clarify.option_texts(doc) == ("שומה - רחוב הדקל 5", "03/04/2021")
    assert clarify.option_texts({"document_id": "d3", "title": "שומה ללא תאריך", "details": {"title": "שומה ללא תאריך"}}) == (
        "שומה ללא תאריך", "")


def test_card_details_take_the_first_quote_of_each_field_and_name_an_untitled_unit():
    fields = [{"field": "name", "quote": "מגדל  הדקלים"}, {"field": "address", "quote": "רחוב הדקל 5"}, {"field": "address", "quote": "רחוב אחר 1"},
              {"field": "city", "quote": "עיר-דוגמה"}, {"field": "date_text", "quote": "03/04/2021"}, {"field": "party_name", "quote": "חברה"}]
    details = contexts.card_details(fields)
    assert details == {"name": "מגדל הדקלים", "address": "רחוב הדקל 5", "city": "עיר-דוגמה", "date_text": "03/04/2021"}
    assert contexts.untitled_label(details) == "מגדל הדקלים"
    assert contexts.untitled_label({"address": "רחוב הדקל 5", "city": "עיר-דוגמה"}) == "רחוב הדקל 5, עיר-דוגמה"
    assert contexts.untitled_label({"address": "רחוב הדקל 5, עיר-דוגמה", "city": "עיר-דוגמה"}) == "רחוב הדקל 5, עיר-דוגמה"
    assert contexts.untitled_label({"block_parcel": "גוש 4001 חלקה 7"}) == "גוש 4001 חלקה 7"
    assert contexts.untitled_label({}) == ""


def test_the_label_of_a_candidate_names_an_untitled_unit_by_its_details_and_is_unchanged_without_them():
    plain = {"document_id": "d1", "title": "קובץ", "file_title": "קובץ",
             "unit": {"display": "מתחם הגפן א", "pages": [1, 1], "identifiers": []}}
    assert entities.candidate_label(plain) == "«מתחם הגפן א» (עמוד 1; בקובץ «קובץ»)"
    bare = {**plain, "unit": {"display": None, "pages": [3, 3], "identifiers": []}}
    assert entities.candidate_label(bare) == f"«{contexts.MSG_NO_TITLE}» (עמוד 3; בקובץ «קובץ»)"  # no details: as before
    told = {**bare, "details": {"address": "רחוב הצאלון 8", "date": "01/02/2023"}}
    assert entities.candidate_label(told) == "«רחוב הצאלון 8» (עמוד 3; 01/02/2023; בקובץ «קובץ»)"


# --- the repeat rule and the cap (R10) ------------------------------------------------------------------------------


def state(**kw):
    return clarify.ClarificationState(**kw)


def test_a_kind_already_resolved_for_the_question_is_a_repeat_and_two_in_a_row_cap_it():
    assert clarify.violation("unit", state(resolved={"unit": "א"}, consecutive=1), {"x", "y"}) == "repeat"
    assert clarify.violation("period", state(resolved={"unit": "א"}, consecutive=1), {"x", "y"}) is None
    assert clarify.violation("period", state(resolved={}, consecutive=2), {"x"}) == "cap"
    assert clarify.violation("unit", state(resolved={"unit": "א"}, consecutive=2), {"x"}) == "cap"  # the cap first
    assert clarify.violation("unit", None, {"x"}) is None and clarify.violation("unit", state(), {"x"}) is None
    assert clarify.violation("text", state(resolved={"unit": "א"}, consecutive=1), set()) is None


def test_a_narrowing_to_the_options_a_reply_matched_is_not_a_repeat_but_still_counts_toward_the_cap():
    narrowing = state(resolved={"unit": "א"}, consecutive=1, narrowing=frozenset({"a", "b", "c"}))
    assert clarify.violation("unit", narrowing, {"a", "b"}) is None
    assert clarify.violation("unit", narrowing, {"a", "d"}) == "repeat"  # a new option is no narrowing
    assert clarify.violation("unit", narrowing, set()) == "repeat"
    capped = state(resolved={"unit": "א"}, consecutive=2, narrowing=frozenset({"a", "b"}))
    assert clarify.violation("unit", capped, {"a", "b"}) == "cap"
    assert clarify.violation("unit", capped, {"a", "b"}, check_cap=False) is None  # a missing parameter is not capped


def test_the_reprompt_names_the_choice_and_asks_for_the_answer_once():
    text = clarify.reprompt("repeat", "unit", state(resolved={"unit": "שומה <b>א</b>"}, consecutive=1))
    assert "שומה" in text and "אל תשאל שוב" in text and "<b>" not in text  # a document's label is never markup in the prompt
    assert "שתי שאלות הבהרה רצופות" in clarify.reprompt("cap", "unit", state(consecutive=2))
    assert "הנחות" in clarify.reprompt("cap", "unit", state(consecutive=2))


def test_a_clarification_the_question_did_not_need_gets_a_reason_specific_reprompt():
    assert "הנתונים העדכניים ביותר קודם" in clarify.reprompt("time_preference_decides", "period", None)
    assert "היחידה כבר נקבעה" in clarify.reprompt("unit_pinned", "unit", None)
    assert "שתי הקריאות בקצרה" in clarify.reprompt("phrase_not_in_sources", "area_basis", None)
    assert set(clarify.MSG_NOT_NEEDED) == {"time_preference_decides", "unit_pinned", "phrase_not_in_sources"}
