"""Turn interpretation: rules fast path, model interpreter and limited mode (U4, KTD1, KTD2, R1-R4, R20)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from app.answering.interpret import (
    INTERPRET_INSTRUCTIONS,
    answer_plan,
    build_interpret_input,
    interpret,
    interpret_with_model,
    match_clarification_reply,
)
from app.answering.parser import Gazetteer
from app.answering.plan import TurnPlan, validate_plan
from app.answering.state import (
    ConversationState,
    PendingClarification,
    SourceRef,
    StateConditions,
    apply_turn,
)
from app.providers.llm import CallStatus, Purpose, StructuredResult
from tests.support.scripted_provider import ScriptedProvider

GAZ = Gazetteer(cities=["רמת גן", "גבעתיים"],
                neighborhoods=[("רמת גן", "חרוזים"), ("רמת גן", "הבורסה"), ("רמת גן", "נחלת גנים")])
ATTRS = [
    {"handle": "A1", "key": "price_per_sqm", "label": "מחיר למ״ר", "aliases": [], "unit_dimension": "money_per_area",
     "source": "structured"},
    {"handle": "A2", "key": "safe_room_area", "label": "שטח ממ״ד", "aliases": ["ממ״ד"], "unit_dimension": "area",
     "source": "extracted"},
]
DATA_KIND_OPTIONS = [{"value": "transaction_price", "label": "מחירי עסקאות"},
                     {"value": "appraised_value", "label": "שווי שנקבע בשומות"}]
COMPUTE = {"tool": "compute_records", "attribute_handle": None, "source_handles": []}


def pending_state() -> ConversationState:
    pending = PendingClarification(key="data_kind", question="לאיזה נתון הכוונה?", options=DATA_KIND_OPTIONS,
                                   original_question="מה המחיר הממוצע למ״ר ברמת גן ב-2024?", task_type="compute",
                                   metric="mean", conditions=StateConditions(city="רמת גן", year_from=2024))
    return ConversationState(task_type="clarify", pending=pending, conditions=pending.conditions, version=2)


def monetary_state(year: int = 2024) -> ConversationState:
    return ConversationState(
        task_type="compute", metric="mean", version=7,
        conditions=StateConditions(data_kind="transaction_price", date_field="transaction_date", year_from=year,
                                   city="רמת גן", neighborhood="חרוזים", area_type="net", property_type="apartment",
                                   vat_basis="included"))


def run(question, state, provider=None):
    return interpret(question, state, GAZ, ATTRS, provider)


# --- clarification replies (AE5) ------------------------------------------------------------------------

def test_free_text_clarification_answer_through_the_model():
    provider = ScriptedProvider().on(Purpose.INTERPRET, TurnPlan.build(
        task_type="compute", turn_relation="answer_to_clarification", clarification_answer="transaction_price",
        steps=[COMPUTE]))
    state = pending_state()
    result = interpret_with_model(provider, "התכוונתי לעסקאות", state, GAZ, ATTRS)
    assert result.ok and result.mode == "model" and result.status == CallStatus.OK
    new, effects = apply_turn(state, result.plan, question="התכוונתי לעסקאות")
    assert effects.resolved == ("data_kind", "transaction_price") and new.pending is None
    assert (new.conditions.data_kind, new.conditions.city, new.conditions.year_from) == ("transaction_price", "רמת גן",
                                                                                         2024)
    call = provider.calls[0]
    assert call.purpose == Purpose.INTERPRET and call.schema is TurnPlan
    assert json.loads(call.input)["pending_clarification"]["options"] == DATA_KIND_OPTIONS


def test_free_text_clarification_answer_in_limited_mode_by_label_overlap():
    state = pending_state()
    assert match_clarification_reply("התכוונתי לעסקאות", state.pending) == "transaction_price"
    assert match_clarification_reply("שווי בשומות", state.pending) == "appraised_value"
    # between two options, rejecting one names the other ("not transactions" = the appraised values)
    assert match_clarification_reply("לא עסקאות", state.pending) == "appraised_value"
    assert match_clarification_reply("מה מחיר העסקאות בגבעתיים ב-2023?", state.pending) is None
    result = run("התכוונתי לעסקאות", state)
    assert result.mode == "rules" and result.plan.turn_relation == "answer_to_clarification"
    new, effects = apply_turn(state, result.plan, question="התכוונתי לעסקאות")
    assert effects.resolved == ("data_kind", "transaction_price") and new.conditions.data_kind == "transaction_price"
    assert new.metric == "mean" and new.task_type == "compute"


@pytest.mark.parametrize("scripted", [False, True])
def test_unrelated_question_keeps_the_pending_clarification(scripted):
    state = pending_state()
    question = "אילו שומות מזכירות היתר בנייה?"
    provider = None
    if scripted:
        provider = ScriptedProvider().on(Purpose.INTERPRET, TurnPlan.build(
            task_type="locate", turn_relation="new_question", topic="היתר בנייה", search_queries=["היתר בנייה"],
            steps=[{"tool": "locate", "attribute_handle": None, "source_handles": []}]))
    result = run(question, state, provider)
    assert result.mode == ("model" if scripted else "limited")
    assert result.plan.turn_relation == "new_question"
    new, effects = apply_turn(state, result.plan, question=question)
    assert new.pending == state.pending and effects.resolved is None


def test_a_self_contained_question_the_model_reads_as_a_reply_stays_a_new_question():
    """Found with the real model: "מה שטח המחסן בדירה ברחוב האירוסים 12?" asked while a data-kind
    clarification was open came back as an answer to it, and the clarification was lost."""
    provider = ScriptedProvider().on(Purpose.INTERPRET, TurnPlan.build(
        task_type="answer", turn_relation="answer_to_clarification", clarification_answer="transaction_price",
        search_queries=["שטח המחסן"], steps=[{"tool": "search", "attribute_handle": None, "source_handles": []}]))
    state = pending_state()
    question = "מה שטח המחסן בדירה ברחוב האירוסים 12?"
    result = interpret_with_model(provider, question, state, GAZ, ATTRS)
    assert result.plan.turn_relation == "new_question" and result.plan.clarification_answer is None
    new, effects = apply_turn(state, result.plan, question=question)
    assert new.pending == state.pending and effects.resolved is None


# --- AE1: a non-monetary average never becomes a price question --------------------------------------

def test_safe_room_average_in_limited_mode_searches_content_without_price_clarification():
    result = run("מה גודל ממ״ד ממוצע ברמת גן?", ConversationState())
    assert result.mode == "limited" and result.limitation
    plan = result.plan
    assert plan.task_type == "answer" and [s.tool for s in plan.steps] == ["search"]
    assert plan.conditions.data_kind is None and plan.clarification is None
    assert plan.conditions.city == "רמת גן"
    _, effects = apply_turn(ConversationState(), plan)
    assert effects.clarification is None


def test_safe_room_average_goes_to_the_model_when_cloud_is_on():
    provider = ScriptedProvider().on(Purpose.INTERPRET, TurnPlan.build(
        task_type="compute", topic="גודל ממ״ד", metric="mean",
        attribute={"handle": "A2", "description": "גודל ממ״ד", "unit_dimension": "area"},
        conditions={"city": "רמת גן"}, search_queries=["שטח ממ״ד ברמת גן"],
        steps=[{"tool": "extract_and_compute", "attribute_handle": "A2", "source_handles": []}]))
    result = run("מה גודל ממ״ד ממוצע ברמת גן?", ConversationState(), provider)
    assert result.mode == "model" and len(provider.calls) == 1
    assert result.plan.conditions.data_kind is None and result.plan.clarification is None


# --- rules fast path ---------------------------------------------------------------------------------

def test_fully_explained_monetary_question_makes_no_model_call():
    provider = ScriptedProvider()
    result = run("מה המחיר הממוצע למ״ר לעסקאות ברמת גן ב-2024", ConversationState(), provider)
    assert result.mode == "rules" and provider.calls == []
    plan = result.plan
    assert plan.task_type == "compute" and [s.tool for s in plan.steps] == ["compute_records"]
    assert (plan.conditions.data_kind, plan.conditions.city, plan.conditions.year_from) == (
        "transaction_price", "רמת גן", 2024)


def _combined_eval_questions():
    path = Path(__file__).resolve().parents[2] / "eval" / "questions.yaml"
    items = yaml.safe_load(path.read_text(encoding="utf-8"))["items"]
    return [(i["id"], i["turns"][0]["ask"], i["turns"][0]["expect"]["filters"]) for i in items
            if i["category"] == "combined"]


@pytest.mark.parametrize("item_id, question, filters", _combined_eval_questions())
def test_combined_eval_items_keep_the_numeric_part_without_a_model(item_id, question, filters):
    result = run(question, ConversationState())
    assert result.mode == "rules", item_id
    plan = result.plan
    assert plan.task_type == "compute_explain"
    assert [s.tool for s in plan.steps] == ["compute_records", "search"]
    assert len(plan.search_queries) == 1 and result.parsed.explanation_clause == plan.search_queries[0]
    for key in ("data_kind", "date_field", "year_from", "city"):
        assert getattr(plan.conditions, key) == filters.get(key), (item_id, key)


def test_follow_up_year_after_a_monetary_answer_changes_only_the_year():
    state = monetary_state(2024)
    provider = ScriptedProvider()
    result = run("ומה לגבי 2023?", state, provider)
    assert result.mode == "rules" and provider.calls == []
    assert result.plan.turn_relation == "follow_up" and result.plan.task_type == "compute"
    new, effects = apply_turn(state, result.plan, question="ומה לגבי 2023?")
    assert effects.changed == ("year_from",)
    assert new.conditions.model_dump() == state.conditions.model_copy(update={"year_from": 2023}).model_dump()


def test_previous_year_by_rules_resolves_against_the_starting_state():
    state = monetary_state(2023)
    result = run("ומה לגבי השנה הקודמת?", state)
    assert result.mode == "rules" and result.plan.conditions.relative_year_offset == -1
    first, _ = apply_turn(state, result.plan)
    second, _ = apply_turn(state, result.plan)
    assert first.conditions.year_from == second.conditions.year_from == 2022


def test_unknown_place_abstains():
    result = run("מה מחיר העסקאות למ״ר בחיפה ב-2024 לפי תאריך עסקה?", ConversationState())
    assert result.unknown_place == "חיפה" and result.plan.task_type == "abstain" and result.plan.steps == []


def test_meta_question_in_limited_mode_explains_the_previous_answer():
    state = monetary_state()
    why = run("למה?", state)
    assert why.plan.turn_relation == "meta_why" and [s.tool for s in why.plan.steps] == ["explain_previous"]
    sources = run("תראה לי את המקור", state)
    assert sources.plan.turn_relation == "meta_sources" and [s.tool for s in sources.plan.steps] == ["show_sources"]


# --- model failures and invalid plans fall back to limited mode ---------------------------------------

def test_provider_failure_is_typed_and_falls_back_to_limited_mode():
    provider = ScriptedProvider().on(Purpose.INTERPRET, CallStatus.TIMEOUT)
    direct = interpret_with_model(provider, "מה נכתב על השיפוץ?", ConversationState(), GAZ, ATTRS)
    assert not direct.ok and direct.status == CallStatus.TIMEOUT
    provider = ScriptedProvider().on(Purpose.INTERPRET, CallStatus.TIMEOUT)
    result = run("מה נכתב על השיפוץ?", ConversationState(), provider)
    assert result.mode == "limited" and result.status == CallStatus.TIMEOUT and result.limitation
    assert [s.tool for s in result.plan.steps] == ["search"]


@pytest.mark.parametrize("bad", [
    TurnPlan.build(task_type="compute", attribute={"handle": "A9", "description": None, "unit_dimension": None}),
    TurnPlan.build(search_queries=["SELECT * FROM documents"], steps=[{"tool": "search", "attribute_handle": None,
                                                                         "source_handles": []}]),
])
def test_invalid_model_plan_is_rejected_and_limited_mode_answers(bad):
    provider = ScriptedProvider().on(Purpose.INTERPRET, bad)
    result = run("מה נכתב על השיפוץ?", ConversationState(), provider)
    assert result.mode == "limited" and result.status == CallStatus.INVALID and result.errors


@pytest.mark.parametrize("repairable", [
    TurnPlan.build(steps=[{"tool": "show_sources", "attribute_handle": None, "source_handles": ["S4"]}]),
    TurnPlan.build(steps=[COMPUTE] * 5),
    TurnPlan.build(task_type="compare", steps=[{"tool": "compare", "attribute_handle": None,
                                                "source_handles": ["S1", "S2"]}]),
])
def test_repairable_model_plan_is_used_not_sent_to_limited_mode(repairable):
    """A handle the server never issued, or too many steps, is repaired (dropped, truncated): the model's plan
    still runs instead of the limited path (the cause of a real-model 'invalid' plan)."""
    provider = ScriptedProvider().on(Purpose.INTERPRET, repairable)
    result = run("השווה בין שתי השומות", ConversationState(), provider)
    assert result.mode == "model" and result.status == CallStatus.OK
    assert all(not s.source_handles for s in result.plan.steps) and len(result.plan.steps) <= 4


def test_model_plan_with_extra_field_is_invalid():
    raw = TurnPlan.build().model_dump() | {"sql": "SELECT 1"}
    provider = ScriptedProvider().on(Purpose.INTERPRET, raw)
    result = interpret_with_model(provider, "מה נכתב על השיפוץ?", ConversationState(), GAZ, ATTRS)
    assert not result.ok and result.status == CallStatus.INVALID


def test_model_place_outside_the_gazetteer_abstains():
    provider = ScriptedProvider().on(Purpose.INTERPRET, TurnPlan.build(
        task_type="compute", conditions={"city": "אילת"}, metric="mean", steps=[COMPUTE]))
    result = run("מה גודל המחסן הממוצע באילת?", ConversationState(), provider)
    assert result.mode == "model" and result.unknown_place == "אילת" and result.plan.task_type == "abstain"


# --- prompt ------------------------------------------------------------------------------------------

def test_prompt_carries_structure_and_handles_only():
    state = monetary_state().model_copy(update={
        "sources": {"S1": SourceRef(document_id="11111111-2222-3333-4444-555555555555", version_id="v-1", page=3)},
        "recent_questions": ["ש1", "ש2", "ש3"]})
    payload = json.loads(build_interpret_input("ומה לגבי גבעתיים?", state, GAZ, ATTRS))
    assert payload["question"] == "ומה לגבי גבעתיים?"
    assert payload["state"]["source_handles"] == ["S1"]
    assert payload["state"]["conditions"]["neighborhood"] == "חרוזים"
    assert payload["recent_questions"] == ["ש1", "ש2", "ש3"]
    assert {"רמת גן", "גבעתיים"} <= set(payload["places"]["cities"])
    assert [a["handle"] for a in payload["attributes"]] == ["A1", "A2"]
    assert all("key" not in a for a in payload["attributes"])
    raw = json.dumps(payload, ensure_ascii=False)
    assert "11111111-2222" not in raw and "v-1" not in raw
    assert "אל תענה" in INTERPRET_INSTRUCTIONS and "SQL" in INTERPRET_INSTRUCTIONS


def test_rules_and_limited_plans_for_every_eval_question_pass_validation():
    path = Path(__file__).resolve().parents[2] / "eval" / "questions.yaml"
    for item in yaml.safe_load(path.read_text(encoding="utf-8"))["items"]:
        state = ConversationState()
        for turn in item["turns"]:
            result = run(turn["ask"], state)
            assert result.mode in ("rules", "limited"), item["id"]
            check = validate_plan(result.plan, gazetteer=GAZ, attributes=ATTRS, source_handles=state.sources)
            assert check.ok, (item["id"], turn["ask"], check.errors)
            assert result.plan.clarification is None
            state, _ = apply_turn(state, check.plan, question=turn["ask"])


# --- usage of the interpreter call --------------------------------------------------------------------

def test_interpretation_carries_the_calls_tokens_and_latency():
    plan = TurnPlan.build(search_queries=["שיפוץ"], steps=[{"tool": "search", "attribute_handle": None,
                                                           "source_handles": []}])
    provider = ScriptedProvider().on(Purpose.INTERPRET, StructuredResult(
        CallStatus.OK, plan, input_tokens=120, output_tokens=30, latency_ms=900))
    result = run("מה נכתב על השיפוץ?", ConversationState(), provider)
    assert result.mode == "model" and (result.input_tokens, result.output_tokens, result.latency_ms) == (120, 30, 900)
    provider = ScriptedProvider().on(Purpose.INTERPRET, StructuredResult(CallStatus.TIMEOUT, latency_ms=4000))
    result = run("מה נכתב על השיפוץ?", ConversationState(), provider)
    assert result.mode == "limited" and result.status == CallStatus.TIMEOUT and result.latency_ms == 4000
    rules = run("מחיר למ״ר בעסקאות שנחתמו ב-2024 בחרוזים", ConversationState(), None)
    assert (rules.input_tokens, rules.output_tokens, rules.latency_ms) == (None, None, None)


def test_answer_plan_resumes_the_interrupted_task():
    pending = PendingClarification(key="data_kind", question="לאיזה נתון הכוונה?",
                                   options=DATA_KIND_OPTIONS, original_question="מחיר למ״ר בחרוזים")
    plan = answer_plan(pending, "transaction_price")
    assert plan.turn_relation == "answer_to_clarification" and plan.clarification_answer == "transaction_price"
    assert [s.tool for s in plan.steps] == ["compute_records"]



# --- counter-examples to "a short text is a reply" (code review, user item 7) ---------------------------------

@pytest.mark.parametrize("text", ["כמה עסקאות היו?", "עסקאות או שומות", "אין עסקאות", "מה עם שומות?"])
def test_short_questions_and_alternatives_are_not_replies(text):
    assert match_clarification_reply(text, pending_state().pending) is None


@pytest.mark.parametrize(("text", "value"), [
    ("עסקאות ולא שומות", "transaction_price"),
    ("התכוונתי למחירי העסקאות בפועל ולא לשווי שנקבע בשומות", "transaction_price"),
    ("שומות", "appraised_value"),
    ("התכוונתי לשומות", "appraised_value"),
    ("בשומות", "appraised_value"),
])
def test_long_or_negated_replies_naming_an_option_are_replies(text, value):
    assert match_clarification_reply(text, pending_state().pending) == value


def test_a_negated_option_label_matches_only_a_negated_reply():
    from app.answering.state import ClarifyOption, PendingClarification
    p = PendingClarification(key="vat_basis", question="כולל מע״מ?", options=[
        ClarifyOption(value="incl", label="כולל מע״מ"), ClarifyOption(value="excl", label="לא כולל מע״מ")])
    assert match_clarification_reply("לא כולל", p) == "excl"
    assert match_clarification_reply("כולל", p) == "incl"


# --- the model path: a turn read as a reply is checked by its structure, not its length -------------------------

def _reply_plan(**kw):
    from app.answering.plan import TurnPlan
    return TurnPlan.build(task_type="compute", turn_relation="answer_to_clarification", **kw)


def test_a_long_reply_that_names_the_option_is_a_reply():
    from app.answering.interpret import reply_is_new_question
    p = pending_state().pending
    plan = _reply_plan(clarification_answer="appraised_value")
    assert not reply_is_new_question(plan, p, "התכוונתי לשווי שנקבע בשומות ולא למחירי העסקאות בפועל, תודה")


def test_a_short_question_with_its_own_condition_is_a_new_question():
    from app.answering.interpret import reply_is_new_question
    p = pending_state().pending
    plan = _reply_plan(clarification_answer="transaction_price", conditions={"city": "חולון"})
    assert reply_is_new_question(plan, p, "עסקאות בחולון?")


def test_a_short_question_naming_no_option_is_a_new_question():
    from app.answering.interpret import reply_is_new_question
    p = pending_state().pending
    assert reply_is_new_question(_reply_plan(clarification_answer="transaction_price"), p, "כמה דירות יש?")


def test_a_reply_that_names_new_entities_is_a_new_question():
    from app.answering.interpret import reply_is_new_question
    p = pending_state().pending
    plan = _reply_plan(clarification_answer="transaction_price", entities=["רחוב הרצל 5"])
    assert reply_is_new_question(plan, p, "עסקאות ברחוב הרצל 5")


def test_choosing_a_source_resumes_the_comparison_with_both_sides():
    """Code review #6: the referent answer used to drop the chosen source, so the comparison never resumed."""
    from app.answering.interpret import answer_plan
    from app.answering.state import ClarifyOption, PendingClarification

    p = PendingClarification(key="referent", question="עם איזה מקור להשוות?", task_type="compare",
                             options=[ClarifyOption(value="S2", label="מקור S2")], source_handles=["S1"])
    plan = answer_plan(p, "S2")
    assert plan.task_type == "compare" and plan.steps[0].tool == "compare"
    assert plan.steps[0].source_handles == ["S1", "S2"]


def test_a_question_sharing_only_a_repository_word_with_the_option_is_a_new_question():
    """Found by the browser run: "אילו שומות מזכירות היתר בנייה?" while the data-kind clarification was open was
    taken as the answer "שווי שנקבע בשומות", because "שומות" is also a word of that option."""
    from app.answering.interpret import reply_is_new_question
    p = pending_state().pending
    plan = _reply_plan(clarification_answer="appraised_value")
    assert reply_is_new_question(plan, p, "אילו שומות מזכירות היתר בנייה?")
    assert not reply_is_new_question(plan, p, "שווי בשומות?")
