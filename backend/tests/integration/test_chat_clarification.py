"""A structured clarification inside the final answer, validated and grounded by the server (round 26 U2; R1-R7, R10;
AE1, AE2, AE5; KTD1, KTD2).

The model is scripted: these tests prove what the server does with a clarification the model sends -- which options it
keeps, how it fills their details from the sources, what it refuses -- not whether a real model asks well (round 26 U7
measures that). The turn is run through ``engine.run_turn`` directly: the record the turn returns
(``TurnOutcome.clarification``) is what the conversation's pending slot stores (U3). Synthetic documents and invented
places only."""

from __future__ import annotations

import re

import pytest

from app.chat import cards, contexts, engine
from app.platform import pipeline
from app.providers.llm import Purpose
from tests.factories import make_office
from tests.support.scripted_agent import ScriptedAgent, call, final, offer
from tests.support.scripted_provider import ScriptedProvider
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

HILL_TITLE = "שומה - הרימון 14 שכונת הגבעה"
BROOK_TITLE = "שומה - הרימון 14 שכונת הנחל"
HILL_DATE, BROOK_DATE = "15/03/2024", "02/09/2022"
BOUND_TITLE = "שומות כרוכות - קובץ סינתטי"
NO_TITLE = contexts.MSG_NO_TITLE
SWITCH_ON = {"version": 1, "prefer_recent": True, "anchor_date": "2026-11-02"}
SWITCH_OFF = {"version": 1, "prefer_recent": False, "anchor_date": "2026-11-02"}


@pytest.fixture(autouse=True)
def no_models(monkeypatch):
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    contexts._CACHE.clear()


def _bound_card_model(instructions: str, input: str) -> dict:
    """What a model reads out of the opening of each unit of the bound file (it answers by the unit's own words)."""
    def subject(kind, quote):
        return {"role": "subject", "field": kind, "quote": quote}

    if "הצאלון 8" in input:
        return {"fields": [subject("address", "רחוב הצאלון 8"), subject("date_text", "01/02/2023")]}
    if "הצאלון 12" in input:
        return {"fields": [subject("address", "רחוב הצאלון 12"), subject("date_text", "20/06/2024")]}
    return {"fields": []}


@pytest.fixture
def world(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    g = a.default_group_id
    a.hill, _ = unit_file(a, g, HILL_TITLE, [{"title": None, "blocks": [
        "שומת מקרקעין להרימון 14 בשכונת הגבעה", f"המועד הקובע לשומה: {HILL_DATE}", "שטח המגרש הוא 612 מ״ר."]}], sha="a" * 64)
    a.brook, _ = unit_file(a, g, BROOK_TITLE, [{"title": None, "blocks": [
        "שומת מקרקעין להרימון 14 בשכונת הנחל", f"המועד הקובע לשומה: {BROOK_DATE}", "שטח המגרש הוא 540 מ״ר."]}], sha="b" * 64)
    a.bound, a.bound_ver = unit_file(a, g, BOUND_TITLE, [
        {"title": "שומה - רחוב השקמה 3", "blocks": ["שומת מקרקעין לרחוב השקמה 3", "המועד הקובע לשומה: 10/10/2021",
                                                    "שטח המגרש הוא 300 מ״ר."]},
        {"title": None, "blocks": ["נכס ברחוב הצאלון 8, נווה גן", "המועד הקובע לשומה: 01/02/2023",
                                   "שטח המגרש הוא 410 מ״ר."]},
        {"title": None, "blocks": ["נכס ברחוב הצאלון 12, נווה גן", "המועד הקובע לשומה: 20/06/2024",
                                   "שטח המגרש הוא 455 מ״ר."]}], sha="c" * 64)
    cards.generate(a.system, a.bound_ver, ScriptedProvider().on(Purpose.IDENTIFY, _bound_card_model, repeat=True))
    a.bases, _ = unit_file(a, g, "שומה - שטחי הדגמה", [{"title": None, "blocks": [
        "שומת מקרקעין לנכס הדגמה", "השטח נמדד בשתי דרכים: שטח ברוטו כולל קירות, ושטח נטו ללא קירות.",
        "הערכת השווי נעשתה לפי שטח נטו."]}], sha="d" * 64)
    a.conflict, _ = unit_file(a, g, "שומה - נכס עם נתונים סותרים", [{"title": None, "blocks": [
        "שומת מקרקעין לנכס הדגמה", "בסיכום השומה נקבע השווי למ״ר: 12,500 ₪ למ״ר.",
        "בטבלת התחשיב מופיע השווי למ״ר: 13,100 ₪ למ״ר."]}], sha="e" * 64)
    return a


def run(world, question: str, steps: list, *, state=None, time_preference=SWITCH_OFF,
        intent: str = "property") -> tuple[engine.TurnOutcome, ScriptedAgent]:
    """One first turn with the scripted model: the outcome and the agent (what it was shown is ``agent.seen``)."""
    agent = ScriptedAgent(steps, intent=intent)
    extra = {} if state is None else {"clarification_state": state}
    inp = engine.TurnInput(question, [], None, [], {}, time_preference=time_preference, **extra)
    return engine.run_turn(world.ctx(), agent, inp, lambda *a: None, lambda: False), agent


def handle_of(items: list, needle: str) -> str:
    """The handle (U#) of the candidate card of the turn's first message whose card line holds ``needle``."""
    handle = None
    for line in items[0]["content"].splitlines():
        if m := re.match(r"- (U\d+) ", line):
            handle = m.group(1)
        if handle and needle in line:
            return handle
    raise AssertionError(f"no candidate card holds {needle!r}")


LIST = [call("find_documents", query="הרימון 14")]


def two_documents(world, *, key_order=(0, 1)) -> tuple[engine.TurnOutcome, ScriptedAgent]:
    asked = "יש שתי שומות להרימון 14. לאיזו התכוונת?"
    docs = [(HILL_TITLE, world.hill), (BROOK_TITLE, world.brook)]
    chosen = [docs[i] for i in key_order]
    return run(world, "מה שטח המגרש בהרימון 14?", [LIST, final(asked, "clarification", clarification=asked, clarify=offer(
        "unit", asked, *chosen, reason="יש שתי שומות באותה כתובת"))])


# --- AE1: two appraisals of one address (R2, R5, R6) -----------------------------------------------------------------------------------


def test_ae1_two_appraisals_of_one_address_give_one_question_with_two_grounded_options(world):
    out, _ = two_documents(world)
    record = out.clarification
    assert out.answer.status == "clarification" and record["kind"] == "unit" and record["allow_all"] is False
    assert record["question"] == "יש שתי שומות להרימון 14. לאיזו התכוונת?" and record["reason"] == "יש שתי שומות באותה כתובת"
    assert [(o["label"], HILL_DATE in o["detail"] or BROOK_DATE in o["detail"]) for o in record["options"]] == [
        (HILL_TITLE, True), (BROOK_TITLE, True)]
    assert HILL_DATE in record["options"][0]["detail"] and BROOK_DATE in record["options"][1]["detail"]
    assert {o["ref"]["document_id"] for o in record["options"]} == {world.hill, world.brook}
    assert record["id"] and out.clarification_check["fallback"] is None
    # the units the reply binds to, in the order shown, and the documents behind the options (their visibility)
    assert [c["document_id"] for c in out.offered_units] == [world.hill, world.brook]
    assert {world.hill, world.brook} <= set(out.shown_documents)


def test_option_ids_are_stable_hashes_of_the_kind_and_the_reference_not_of_the_display_order(world):
    first, _ = two_documents(world)
    second, _ = two_documents(world, key_order=(1, 0))
    ids = {o["label"]: o["id"] for o in first.clarification["options"]}
    assert {o["label"]: o["id"] for o in second.clarification["options"]} == ids
    assert len(set(ids.values())) == 2 and first.clarification["id"] != second.clarification["id"]
    assert all(re.fullmatch(r"[0-9a-f]{12}", i) for i in ids.values())


def test_ae2_a_clear_question_is_answered_directly_with_no_clarification_and_the_same_text(world):
    text = "שטח המגרש הוא 612 מ״ר [S1]."
    out, _ = run(world, "מה שטח המגרש בהרימון 14 בשכונת הגבעה?", [
        [call("search", query="שטח המגרש", document_ids=[world.hill], limit=None)],
        final(text, claims=[{"text": "שטח המגרש הוא 612 מ״ר", "source_ids": ["S1"], "basis": "explicit"}])])
    assert out.answer.status == "answered" and out.clarification is None and out.clarification_check == {}
    assert out.answer.answer_markdown.startswith("שטח המגרש הוא 612")  # the policy and the schema change no answer


# --- options the turn never saw, and fewer than two valid options (KTD2) -------------------------------------------------------------------


def test_an_option_naming_a_document_the_turn_never_saw_is_dropped_and_one_left_means_no_buttons(world):
    asked = "יש שתי שומות להרימון 14. לאיזו התכוונת?"
    out, _ = run(world, "מה שטח המגרש בהרימון 14?", [[call("search", query="שטח המגרש", document_ids=[world.hill], limit=None)],
                 final(asked, "clarification", clarification=asked, clarify=offer(
                     "unit", asked, (HILL_TITLE, world.hill), (BROOK_TITLE, world.brook)))])
    record = out.clarification
    assert record["options"] == [] and record["question"] == asked and record["kind"] == "unit"
    assert out.clarification_check["fallback"] == "too_few_options"
    assert out.clarification_check["dropped"] == [{"key": "2", "reason": "unit_not_offered"}]
    assert out.answer.answer_markdown == asked and out.answer.status == "clarification"


def test_a_dropped_option_is_never_listed_in_the_question_the_user_sees(world):
    listed = f"האם התכוונת ל«{BROOK_TITLE}» או ל«{HILL_TITLE}»?"
    out, _ = run(world, "מה שטח המגרש בהרימון 14?", [[call("search", query="שטח המגרש", document_ids=[world.hill], limit=None)],
                 final(listed, "clarification", clarification=listed, clarify=offer(
                     "unit", listed, (HILL_TITLE, world.hill), (BROOK_TITLE, world.brook)))])
    assert out.clarification["options"] == [] and BROOK_TITLE not in out.clarification["question"]
    assert BROOK_TITLE not in out.answer.clarification_question and BROOK_TITLE not in out.answer.answer_markdown
    assert out.clarification_check["fallback"] == "too_few_options"


def test_a_handle_the_turn_never_offered_is_dropped(world):
    asked = "לאיזו שומה התכוונת?"
    out, _ = run(world, "מה שטח המגרש בהרימון 14?", [LIST, final(asked, "clarification", clarification=asked, clarify=offer(
        "unit", asked, (HILL_TITLE, world.hill), ("שומה שלא הוצעה", "U9")))])
    assert out.clarification["options"] == [] and out.clarification_check["dropped"] == [
        {"key": "2", "reason": "unit_not_offered"}]


def test_identical_copies_of_one_document_collapse_into_one_option_and_one_option_means_no_buttons(world):
    twin, _ = unit_file(world, world.default_group_id, HILL_TITLE, [{"title": None, "blocks": [
        "שומת מקרקעין להרימון 14 בשכונת הגבעה", f"המועד הקובע לשומה: {HILL_DATE}", "שטח המגרש הוא 612 מ״ר."]}],
        sha="f" * 64)
    asked = "לאיזו שומה התכוונת?"
    out, _ = run(world, "מה שטח המגרש בהרימון 14 בשכונת הגבעה?", [LIST, final(
        asked, "clarification", clarification=asked, clarify=offer("unit", asked, (HILL_TITLE, world.hill), (HILL_TITLE, twin)))])
    assert out.clarification["options"] == [] and out.clarification_check["dropped"] == [
        {"key": "2", "reason": "duplicate_option"}]
    assert out.clarification_check["fallback"] == "too_few_options"


def test_more_than_four_options_show_the_first_four_valid_ones(world):
    asked = "לאיזו שומה?"
    docs = []
    for n in range(3):
        d, _ = unit_file(world, world.default_group_id, f"שומה - הרימון 14 שכונה {n}", [{"title": None, "blocks": [
            f"שומת מקרקעין להרימון 14 בשכונה {n}", f"המועד הקובע לשומה: 0{n + 1}/01/2020"]}], sha=str(n) * 64)
        docs.append((f"שומה - הרימון 14 שכונה {n}", d))
    out, _ = run(world, "מה שטח המגרש בהרימון 14?", [LIST, final(asked, "clarification", clarification=asked, clarify=offer(
        "unit", asked, (HILL_TITLE, world.hill), (BROOK_TITLE, world.brook), *docs))])
    assert [o["label"] for o in out.clarification["options"]] == [HILL_TITLE, BROOK_TITLE, docs[0][0], docs[1][0]]


# --- untitled units told apart by their cards (R6) ---------------------------------------------------------------------------------------


def test_two_untitled_units_become_options_labelled_by_address_and_date_never_untitled(world):
    asked = "בקובץ שני נכסים ברחוב הצאלון. לאיזה התכוונת?"

    def step(items):
        return final(asked, "clarification", clarification=asked, clarify=offer(
            "unit", asked, ("אחד", handle_of(items, "הצאלון 8")), ("שני", handle_of(items, "הצאלון 12"))))

    out, _ = run(world, "מה שטח המגרש ברחוב הצאלון?", [step])
    record = out.clarification
    assert [o["label"] for o in record["options"]] == ["רחוב הצאלון 8", "רחוב הצאלון 12"]
    assert "01/02/2023" in record["options"][0]["detail"] and "20/06/2024" in record["options"][1]["detail"]
    assert all(NO_TITLE not in o["label"] + o["detail"] for o in record["options"])
    assert all("עמוד" in o["detail"] and BOUND_TITLE in o["detail"] for o in record["options"])
    assert [c["unit"]["display"] for c in out.offered_units] == [None, None]  # untitled stays untitled in the anchor


# --- AE5: never ask which value is correct (R4) ------------------------------------------------------------------------------------------------


def test_ae5_a_clarification_of_the_kind_value_is_refused_by_the_schema():
    body = final("שאלה", "clarification", clarification="שאלה", clarify=offer(
        "unit", "שאלה", ("12,500 ₪", "U1"), ("13,100 ₪", "U2")))["final"]
    assert engine.FinalAnswer.model_validate(body).clarify.kind == "unit"
    bad = {**body, "clarify": {**body["clarify"], "kind": "value"}}
    with pytest.raises(ValueError):
        engine.FinalAnswer.model_validate(bad)


def test_ae5_options_that_are_values_are_refused_even_under_an_allowed_kind(world):
    asked = "איזה שווי למ״ר נכון?"
    out, _ = run(world, "מה השווי למ״ר?", [[call("search", query="השווי למ״ר", document_ids=[world.conflict], limit=None)],
                 final(asked, "clarification", clarification=asked, clarify=offer(
                     "area_basis", asked, ("12,500 ₪ למ״ר", "12,500 ₪ למ״ר"), ("13,100 ₪ למ״ר", "13,100 ₪ למ״ר")))])
    assert out.clarification["options"] == []
    assert {d["reason"] for d in out.clarification_check["dropped"]} == {"value_option"}
    assert out.clarification_check["fallback"] == "too_few_options"


def test_ae5_the_conflicting_value_turn_answers_with_both_values_and_asks_nothing(world):
    text = ("בשומה שני ערכים לשווי למ״ר: 12,500 ₪ למ״ר בסיכום [S1], ו-13,100 ₪ למ״ר בטבלת התחשיב [S1]. "
            "יש אי-התאמה בין הערכים, והשומה אינה מכריעה ביניהם.")
    out, _ = run(world, "מה השווי למ״ר?", [[call("search", query="השווי למ״ר", document_ids=[world.conflict], limit=None)],
                 final(text, "partial", claims=[{"text": "השווי למ״ר בסיכום הוא 12,500 ₪ למ״ר", "source_ids": ["S1"], "basis": "explicit"},
                                                 {"text": "השווי למ״ר בטבלת התחשיב הוא 13,100 ₪ למ״ר", "source_ids": ["S1"], "basis": "explicit"}])])
    assert out.answer.status != "clarification" and out.clarification is None
    assert "12,500" in out.answer.answer_markdown and "13,100" in out.answer.answer_markdown


def test_the_policy_says_when_to_ask_and_never_which_value_is_correct():
    policy = engine.POLICY
    for rule in ("אל תשאל איזה ערך נכון", "שתי קריאות סבירות", "שתיים עד ארבע אפשרויות", "clarify.kind",
                 "הצג את שתיהן", "ללא כותרת", "allow_all", "שתי שאלות הבהרה רצופות", "תאריך שהמשתמש ציין"):
        assert rule in policy, rule


# --- period: grounded in the dates of the sources, and decided by the time preference (R3) -----------------------------


ANSWER = final("בשומה מצוין מועד קובע [S1].", claims=[{"text": "בשומה מצוין מועד קובע", "source_ids": ["S1"], "basis": "explicit"}])


def _period_turn(world, options, *, prefer_recent=False, question="מה שטח המגרש בהרימון 14?", then=()):
    asked = "לאיזה מועד קובע התכוונת?"
    return run(world, question, [[call("search", query="המועד הקובע שטח המגרש", document_ids=[world.hill, world.brook], limit=None)],
                                 final(asked, "clarification", clarification=asked, clarify=offer("period", asked, *options)), *then],
               time_preference=SWITCH_ON if prefer_recent else SWITCH_OFF)


def last_notice(agent) -> str:
    """The last user-role item the model was sent: the re-prompt, when the turn had one."""
    return [i["content"] for i in agent.seen[-1] if isinstance(i, dict) and i.get("role") == "user"][-1]


def assert_answered_after_one_reprompt(out, agent, words: str) -> None:
    """The clarification was not needed: the one re-prompt step made the model answer, and nothing of a clarification is left."""
    assert out.answer.status != "clarification" and out.answer.clarify is None and out.clarification is None
    assert out.clarification_check == {} and words in last_notice(agent)
    assert sum(words in "\n".join(i["content"] for i in step if isinstance(i, dict) and isinstance(i.get("content"), str))
               for step in agent.seen) >= 1


PERIODS = [("מרץ 2024", "2024-03-15"), ("ספטמבר 2022", "2022-09-02")]


def test_a_period_clarification_the_switch_already_decides_is_reprompted_and_the_answer_follows(world):
    out, agent = _period_turn(world, PERIODS, prefer_recent=True, then=[ANSWER])
    assert_answered_after_one_reprompt(out, agent, "הנתונים העדכניים ביותר קודם")
    assert len(agent.seen) == 3


def test_a_period_clarification_the_questions_date_decides_is_reprompted_and_the_answer_follows(world):
    out, agent = _period_turn(world, PERIODS, question="מה שטח המגרש בהרימון 14 למועד הקובע 15/03/2024?", then=[ANSWER])
    assert_answered_after_one_reprompt(out, agent, "הנתונים העדכניים ביותר קודם")


def test_a_model_that_insists_on_a_period_the_switch_decides_keeps_the_text_question_and_the_refusal_is_recorded(world):
    asked = "לאיזה מועד קובע התכוונת?"
    again = final(asked, "clarification", clarification=asked, clarify=offer("period", asked, *PERIODS))
    out, agent = _period_turn(world, PERIODS, prefer_recent=True, then=[again])
    assert out.answer.status == "clarification" and out.clarification["options"] == [] and len(agent.seen) == 3
    assert out.clarification_check["violation"] == "time_preference_decides"
    assert out.clarification_check["fallback"] == "time_preference_decides_refused"


def test_a_period_clarification_with_the_switch_off_and_no_stated_date_is_not_reprompted(world):
    out, agent = _period_turn(world, PERIODS, then=[ANSWER])  # the negative control
    assert out.clarification["kind"] == "period" and len(out.clarification["options"]) == 2 and len(agent.seen) == 2


# --- area basis and method: the phrase is a phrase of a read source (KTD2) ------------------------------------------------------------------------


def _basis_turn(world, kind, options):
    asked = "באיזה בסיס שטח?"
    return run(world, "מה שטח הנכס?", [[call("search", query="שטח ברוטו נטו", document_ids=[world.bases], limit=None)],
                                       final(asked, "clarification", clarification=asked, clarify=offer(kind, asked, *options))])


def test_area_basis_options_with_phrases_from_a_read_source_are_kept(world):
    out, _ = _basis_turn(world, "area_basis", [("שטח ברוטו", "שטח ברוטו"), ("שטח נטו", "שטח נטו")])
    record = out.clarification
    assert record["kind"] == "area_basis" and [o["ref"] for o in record["options"]] == [{"phrase": "שטח ברוטו"}, {"phrase": "שטח נטו"}]
    assert all("שטחי הדגמה" in o["detail"] for o in record["options"])  # where the phrase is written


BASIS_ANSWER = final("הערכת השווי נעשתה לפי שטח נטו [S1].", claims=[
    {"text": "הערכת השווי נעשתה לפי שטח נטו", "source_ids": ["S1"], "basis": "explicit"}])


def _basis_turn_then(world, kind, options, then):
    asked = "באיזה בסיס שטח?"
    return run(world, "מה שטח הנכס?", [[call("search", query="שטח ברוטו נטו", document_ids=[world.bases], limit=None)],
                                       final(asked, "clarification", clarification=asked, clarify=offer(kind, asked, *options)), *then])


def test_an_area_basis_phrase_no_read_source_writes_is_reprompted_to_show_both_readings(world):
    out, agent = _basis_turn_then(world, "area_basis", [("שטח ברוטו", "שטח ברוטו"), ("שטח אקוויוולנטי", "שטח אקוויוולנטי")],
                                  [BASIS_ANSWER])
    assert_answered_after_one_reprompt(out, agent, "שתי הקריאות בקצרה")
    assert len(agent.seen) == 3


def test_method_options_no_read_source_writes_are_reprompted_the_same_way(world):
    out, agent = _basis_turn_then(world, "method", [("ממוצע פשוט", "ממוצע פשוט"), ("ממוצע משוקלל", "ממוצע משוקלל")], [BASIS_ANSWER])
    assert_answered_after_one_reprompt(out, agent, "שתי הקריאות בקצרה")


def test_a_model_that_insists_on_phrases_no_source_writes_keeps_the_text_question_and_the_refusal_is_recorded(world):
    asked = "באיזה בסיס שטח?"
    again = final(asked, "clarification", clarification=asked, clarify=offer(
        "area_basis", asked, ("שטח ברוטו", "שטח ברוטו"), ("שטח אקוויוולנטי", "שטח אקוויוולנטי")))
    out, agent = _basis_turn_then(world, "area_basis", [("שטח ברוטו", "שטח ברוטו"), ("שטח אקוויוולנטי", "שטח אקוויוולנטי")], [again])
    assert out.answer.status == "clarification" and out.clarification["options"] == [] and len(agent.seen) == 3
    assert out.clarification_check["dropped"] == [{"key": "2", "reason": "phrase_not_in_sources"}]
    assert out.clarification_check["violation"] == "phrase_not_in_sources"
    assert out.clarification_check["fallback"] == "phrase_not_in_sources_refused"


def test_two_valid_phrases_beside_a_bad_one_keep_their_buttons_without_a_reprompt(world):
    out, agent = _basis_turn_then(world, "area_basis", [("שטח ברוטו", "שטח ברוטו"), ("שטח נטו", "שטח נטו"),
                                                        ("שטח אקוויוולנטי", "שטח אקוויוולנטי")], [])
    assert len(out.clarification["options"]) == 2 and len(agent.seen) == 2 and out.clarification_check["violation"] is None


# --- a unit clarification on a unit the message already pins (G14) --------------------------------------------------------------------------------


def _pinned_steps(then):
    asked = "לאיזו שומה בקובץ התכוונת?"

    def step(items):
        return final(asked, "clarification", clarification=asked, clarify=offer(
            "unit", asked, ("אחד", handle_of(items, "השקמה 3")), ("שני", handle_of(items, "הצאלון 8"))))

    return [step, [call("search", query="המועד הקובע", document_ids=None, limit=None)], *then]


def test_a_unit_clarification_on_a_unit_the_question_states_is_reprompted_to_answer_for_it(world):
    out, agent = run(world, "מה שטח המגרש ב«שומה - רחוב השקמה 3»?", _pinned_steps([ANSWER]))
    assert_answered_after_one_reprompt(out, agent, "היחידה כבר נקבעה")


def test_a_model_that_insists_on_a_unit_the_question_states_keeps_the_text_question_and_the_refusal_is_recorded(world):
    asked = "לאיזו שומה בקובץ התכוונת?"

    def again(items):
        return final(asked, "clarification", clarification=asked, clarify=offer(
            "unit", asked, ("אחד", handle_of(items, "השקמה 3")), ("שני", handle_of(items, "הצאלון 8"))))

    out, agent = run(world, "מה שטח המגרש ב«שומה - רחוב השקמה 3»?", [again, again])
    assert out.answer.status == "clarification" and out.answer.answer_markdown == asked and out.clarification["options"] == []
    assert out.clarification_check["violation"] == "unit_pinned" and out.clarification_check["fallback"] == "unit_pinned_refused"
    assert len(agent.seen) == 2


# --- repeated kind and the cap of two (R10, KTD2) -----------------------------------------------------------------------------------------------


def state(**kw):
    from app.chat import clarify

    return clarify.ClarificationState(**kw)


STATE_UNIT_RESOLVED = dict(resolved={"unit": HILL_TITLE}, consecutive=1)


def _repeat(world, state, replies):
    asked = "יש שתי שומות להרימון 14. לאיזו התכוונת?"
    again = final(asked, "clarification", clarification=asked, clarify=offer(
        "unit", asked, (HILL_TITLE, world.hill), (BROOK_TITLE, world.brook)))
    return run(world, "מה שטח המגרש בהרימון 14?", [LIST, again, *replies], state=state)


def test_a_repeated_kind_gets_one_reprompt_naming_the_users_choice_and_the_answer_follows(world):
    answer = final("שטח המגרש הוא 612 מ״ר [S2].", claims=[{"text": "שטח המגרש הוא 612 מ״ר", "source_ids": ["S2"], "basis": "explicit"}])
    out, agent = _repeat(world, state(**STATE_UNIT_RESOLVED), [[call("search", query="שטח המגרש", document_ids=[world.hill], limit=None)], answer])
    assert out.answer.status == "answered" and out.clarification is None
    notice = [i["content"] for i in agent.seen[-1] if isinstance(i, dict) and i.get("role") == "user"][-1]
    assert HILL_TITLE in notice and "אל תשאל שוב" in notice
    assert len(agent.seen) == 4  # the list, the repeated question, the re-prompt's tool step, the answer


def test_a_model_that_insists_after_the_reprompt_keeps_the_text_question_with_no_buttons(world):
    asked = "יש שתי שומות להרימון 14. לאיזו התכוונת?"
    insist = final(asked, "clarification", clarification=asked, clarify=offer(
        "unit", asked, (HILL_TITLE, world.hill), (BROOK_TITLE, world.brook)))
    out, agent = _repeat(world, state(**STATE_UNIT_RESOLVED), [insist])
    assert out.answer.status == "clarification" and out.clarification["options"] == []
    assert out.clarification_check["fallback"] == "repeat_refused" and len(agent.seen) == 3


def test_a_third_consecutive_clarification_is_refused_the_same_way(world):
    capped = state(resolved={}, consecutive=2)
    asked = "באיזה בסיס שטח?"
    ask = final(asked, "clarification", clarification=asked, clarify=offer(
        "area_basis", asked, ("שטח ברוטו", "שטח ברוטו"), ("שטח נטו", "שטח נטו")))
    answer = final("הערכת השווי נעשתה לפי שטח נטו [S1].", claims=[{"text": "הערכת השווי נעשתה לפי שטח נטו", "source_ids": ["S1"], "basis": "explicit"}])
    search = [call("search", query="שטח ברוטו נטו", document_ids=[world.bases], limit=None)]
    out, agent = run(world, "מה שטח הנכס?", [search, ask, answer], state=capped)
    assert out.answer.status == "answered" and out.clarification is None
    notice = [i["content"] for i in agent.seen[-1] if isinstance(i, dict) and i.get("role") == "user"][-1]
    assert "שתי שאלות הבהרה רצופות" in notice and "הנחות" in notice
    again, _ = run(world, "מה שטח הנכס?", [search, ask, ask], state=capped)  # the model insists: no buttons
    assert again.clarification["options"] == [] and again.clarification_check["fallback"] == "cap_refused"


def test_a_narrowing_among_the_options_a_reply_matched_is_exempt_from_the_repeat_rule(world):
    first, _ = two_documents(world)
    ids = frozenset(o["id"] for o in first.clarification["options"])
    out, agent = _repeat(world, state(resolved={"unit": "הרימון 14"}, consecutive=1, narrowing=ids), [])
    assert out.clarification["kind"] == "unit" and len(out.clarification["options"]) == 2 and len(agent.seen) == 2
    # ... but a superset of what the reply matched is a repeat
    subset = state(resolved={"unit": "הרימון 14"}, consecutive=1, narrowing=frozenset(list(ids)[:1]))
    refused, _ = _repeat(world, subset, [final("בדיקה.", "not_found")])
    assert refused.answer.status != "clarification"  # re-prompted, and then it answered


def test_the_cap_does_not_apply_to_a_clarification_that_asks_for_a_missing_parameter(world, monkeypatch):
    """A calculation's missing rate is asked as it always was (round 7 KTD9): never answered with an assumption."""
    asked = "מהו שיעור העלייה של העלויות בתרחיש?"
    monkeypatch.setattr(engine, "_pending", lambda ws, turn, answer: {"component": "N1", "parameters": ["שיעור"]})
    out, agent = run(world, "מה השווי בתרחיש?", [final(asked, "clarification", clarification=asked)],
                     state=state(resolved={}, consecutive=2))
    assert out.answer.status == "clarification" and len(agent.seen) == 1  # no re-prompt


def test_a_third_text_clarification_is_reprompted_like_any_other(world):
    asked = "על איזה נכס אתה שואל?"
    out, agent = run(world, "מה השטח?", [final(asked, "clarification", clarification=asked), final(
        "לא נמצא מידע מספיק.", "not_found")], state=state(resolved={}, consecutive=2))
    assert out.answer.status == "not_found" and len(agent.seen) == 2


def test_a_text_clarification_with_no_options_is_recorded_as_such(world):
    asked = "על איזה נכס אתה שואל?"
    out, _ = run(world, "מה השטח?", [final(asked, "clarification", clarification=asked)])
    assert out.clarification["kind"] == "text" and out.clarification["options"] == [] and out.clarification["question"] == asked
    assert out.clarification_check["fallback"] == "no_offer"


# --- the question never lists what was dropped, whatever survives (R6); other shapes ---------------------------------------------------------------


def test_a_dropped_option_listed_in_the_question_gets_the_neutral_words_even_when_two_options_survive(world):
    other, _ = unit_file(world, world.default_group_id, "שומה - נכס אחר בעיר אחרת", [{"title": None, "blocks": [
        "שומת מקרקעין לנכס אחר", "המועד הקובע לשומה: 11/11/2020"]}], sha="9" * 64)  # the turn never lists it
    listed = f"האם התכוונת ל«{HILL_TITLE}», ל«{BROOK_TITLE}» או ל«שומה - נכס אחר בעיר אחרת»?"
    out, _ = run(world, "מה שטח המגרש בהרימון 14?", [LIST, final(listed, "clarification", clarification=listed, clarify=offer(
        "unit", listed, (HILL_TITLE, world.hill), (BROOK_TITLE, world.brook), ("שומה - נכס אחר בעיר אחרת", other)))])
    assert [o["label"] for o in out.clarification["options"]] == [HILL_TITLE, BROOK_TITLE]
    assert "נכס אחר" not in out.clarification["question"] and "נכס אחר" not in out.answer.answer_markdown
    assert out.clarification["question"] == out.answer.clarification_question
    assert out.clarification_check["dropped"] == [{"key": "3", "reason": "unit_not_offered"}]


def test_a_clarify_object_on_an_answer_that_is_not_a_clarification_is_ignored(world):
    out, _ = run(world, "מה שטח המגרש בהרימון 14 בשכונת הגבעה?", [
        [call("search", query="שטח המגרש", document_ids=[world.hill], limit=None)],
        final("שטח המגרש הוא 612 מ״ר [S1].", claims=[{"text": "שטח המגרש הוא 612 מ״ר", "source_ids": ["S1"], "basis": "explicit"}],
              clarify=offer("unit", "?", (HILL_TITLE, world.hill), (BROOK_TITLE, world.brook)))])
    assert out.answer.status == "answered" and out.clarification is None and out.offered_units == []


def test_the_same_phrases_in_another_order_keep_their_ids(world):
    first, _ = _basis_turn(world, "area_basis", [("שטח ברוטו", "שטח ברוטו"), ("שטח נטו", "שטח נטו")])
    second, _ = _basis_turn(world, "area_basis", [("נטו", "שטח נטו"), ("ברוטו", "שטח ברוטו")])
    assert {o["ref"]["phrase"]: o["id"] for o in first.clarification["options"]} == {
        o["ref"]["phrase"]: o["id"] for o in second.clarification["options"]}
    assert [o["ref"]["phrase"] for o in second.clarification["options"]] == ["שטח נטו", "שטח ברוטו"]
