"""Conversation state and the pure ``apply_turn`` (U4, KTD12, R17, R18, R20)."""

from __future__ import annotations

from app.answering.plan import TurnPlan
from app.answering.state import (
    ConversationState,
    PendingClarification,
    SourceRef,
    StateConditions,
    apply_turn,
    remember_sources,
)

DATA_KIND_OPTIONS = [{"value": "transaction_price", "label": "מחירי עסקאות"},
                     {"value": "appraised_value", "label": "שווי שנקבע בשומות"}]


def monetary_state(**conditions) -> ConversationState:
    base = {"data_kind": "transaction_price", "date_field": "transaction_date", "year_from": 2023,
            "city": "רמת גן", "neighborhood": "חרוזים", "area_type": "net"}
    return ConversationState(task_type="compute", topic="מחיר למ״ר", metric="mean",
                             conditions=StateConditions(**(base | conditions)), version=4)


def test_previous_year_changes_only_the_year_and_is_idempotent():
    """AE3: 2023 -> 2022, other conditions unchanged; applying the same turn again from the same state
    version gives 2022 again."""
    state = monetary_state()
    plan = TurnPlan.build(task_type="compute", turn_relation="follow_up",
                          conditions={"relative_year_offset": -1},
                          steps=[{"tool": "compute_records", "attribute_handle": None, "source_handles": []}])
    first, effects = apply_turn(state, plan, question="ומה לגבי השנה הקודמת?")
    again, _ = apply_turn(state, plan, question="ומה לגבי השנה הקודמת?")
    assert first.conditions.year_from == 2022
    assert first.conditions.model_dump(exclude={"year_from"}) == state.conditions.model_dump(exclude={"year_from"})
    assert first == again and first.version == state.version + 1
    assert effects.changed == ("year_from",) and effects.clarification is None and effects.base_version == 4
    assert first.task_type == "compute" and first.metric == "mean"


def test_relative_year_shifts_a_stored_range():
    state = monetary_state(year_from=2023, year_to=2024)
    plan = TurnPlan.build(task_type="compute", turn_relation="follow_up", conditions={"relative_year_offset": -1})
    new, _ = apply_turn(state, plan)
    assert (new.conditions.year_from, new.conditions.year_to) == (2022, 2023)


def test_relative_year_without_a_stored_year_asks():
    state = monetary_state(year_from=None)
    plan = TurnPlan.build(task_type="compute", turn_relation="follow_up", conditions={"relative_year_offset": -1})
    new, effects = apply_turn(state, plan)
    assert effects.clarification is not None and effects.clarification.key == "referent"
    assert new.pending is not None and new.task_type == "clarify"
    assert new.conditions.year_from is None


def test_topic_change_clears_place_attribute_and_metric():
    """AE4: nothing from the balcony question in Givatayim survives a change of topic."""
    state = ConversationState(
        task_type="compute", topic="שטח מרפסות", entities=["מרפסת"], metric="mean", unit="sqm",
        attribute={"handle": "A3", "description": "שטח מרפסת", "unit_dimension": "area"},
        conditions=StateConditions(city="גבעתיים", year_from=2024),
        sources={"S1": SourceRef(document_id="d1", version_id="v1", page=2)})
    plan = TurnPlan.build(task_type="locate", turn_relation="topic_change", topic="היתר בנייה",
                          search_queries=["שומות שמזכירות היתר בנייה"],
                          steps=[{"tool": "locate", "attribute_handle": None, "source_handles": []}])
    new, effects = apply_turn(state, plan, question="עכשיו בנושא אחר: אילו שומות מזכירות היתר בנייה?")
    assert new.conditions == StateConditions()
    assert new.attribute is None and new.metric is None and new.unit is None and new.entities == []
    assert new.topic == "היתר בנייה" and new.task_type == "locate" and effects.topic_changed
    assert {"city", "year_from"} <= set(effects.cleared)


def test_follow_up_changes_only_what_it_states():
    state = monetary_state()
    plan = TurnPlan.build(task_type="compute", turn_relation="follow_up", conditions={"city": "גבעתיים"})
    new, effects = apply_turn(state, plan)
    assert new.conditions.city == "גבעתיים" and new.conditions.neighborhood is None  # a new city drops the old hood
    assert new.conditions.year_from == 2023 and new.conditions.data_kind == "transaction_price"
    assert "neighborhood" in effects.cleared


def test_attribute_change_in_a_follow_up_clears_its_dependents_but_keeps_the_place():
    state = monetary_state()
    plan = TurnPlan.build(task_type="compute", turn_relation="follow_up",
                          attribute={"handle": "A2", "description": "שטח ממ״ד", "unit_dimension": "area"},
                          steps=[{"tool": "extract_and_compute", "attribute_handle": "A2", "source_handles": []}])
    new, effects = apply_turn(state, plan)
    assert new.attribute.handle == "A2" and new.metric is None
    assert new.conditions.data_kind is None and new.conditions.area_type is None
    assert new.conditions.city == "רמת גן" and new.conditions.year_from == 2023
    assert "data_kind" in effects.cleared


def test_new_question_starts_from_fresh_conditions():
    state = monetary_state()
    plan = TurnPlan.build(task_type="compute", turn_relation="new_question", conditions={"city": "גבעתיים"},
                          metric="count")
    new, _ = apply_turn(state, plan)
    assert new.conditions == StateConditions(city="גבעתיים") and new.metric == "count"


def test_compare_with_one_referenced_source_asks_which_second_appraisal():
    state = ConversationState(task_type="answer", sources={"S1": SourceRef(document_id="d1", version_id="v1", page=1)})
    plan = TurnPlan.build(task_type="compare", turn_relation="follow_up",
                          steps=[{"tool": "compare", "attribute_handle": None, "source_handles": ["S1"]}])
    new, effects = apply_turn(state, plan, question="השווה את זה לשומה השנייה")
    assert effects.clarification is not None and effects.clarification.key == "referent"
    assert "שומה" in effects.clarification.question
    assert new.pending is not None and new.pending.key == "referent" and new.task_type == "clarify"


def test_compare_with_named_entities_leaves_the_sides_to_the_orchestrator():
    """Named addresses or titles may supply the sides (resolved to documents by the turn, which asks only when
    they do not give two); with neither handles nor entities the referent is asked at once (R10)."""
    named = TurnPlan.build(task_type="compare", entities=["רחוב הדקל 4"],
                           steps=[{"tool": "compare", "attribute_handle": None, "source_handles": []}])
    new, effects = apply_turn(ConversationState(), named, question="השווה בין גרסאות השומה ברחוב הדקל 4")
    assert effects.clarification is None and new.entities == ["רחוב הדקל 4"]
    bare = named.model_copy(update={"entities": []})
    _, effects = apply_turn(ConversationState(), bare, question="השווה בין שתי השומות")
    assert effects.clarification is not None and effects.clarification.key == "referent"
    follow = TurnPlan.build(task_type="compare", turn_relation="follow_up",
                            steps=[{"tool": "compare", "attribute_handle": None, "source_handles": []}])
    _, effects = apply_turn(new, follow, question="מה השתנה בין הגרסאות?")
    assert effects.clarification is None  # the entities of the conversation carry over to the follow-up


def test_value_filter_follows_the_attribute():
    from app.answering.plan import ValueFilterSpec

    attr = {"handle": None, "description": "גובה החלל", "unit_dimension": "length"}
    first = TurnPlan.build(task_type="compute", metric="count", attribute=attr,
                           value_filter=ValueFilterSpec(op=">", value="2.7"))
    state, _ = apply_turn(ConversationState(), first)
    assert state.value_filter.op == ">" and state.prompt_view()["value_filter"] == {"op": ">", "value": "2.7"}
    place = TurnPlan.build(task_type="compute", turn_relation="follow_up", conditions={"city": "גבעתיים"})
    kept, _ = apply_turn(state, place)
    assert kept.value_filter == state.value_filter
    other = TurnPlan.build(task_type="compute", turn_relation="follow_up", metric="mean",
                           attribute={**attr, "description": "שטח החלל"})
    changed, effects = apply_turn(kept, other)
    assert changed.value_filter is None and "value_filter" in effects.cleared


def test_compare_with_two_sources_runs():
    state = ConversationState(sources={"S1": SourceRef(document_id="d1", version_id="v1", page=1),
                                       "S2": SourceRef(document_id="d2", version_id="v2", page=3)})
    plan = TurnPlan.build(task_type="compare", turn_relation="follow_up",
                          steps=[{"tool": "compare", "attribute_handle": None, "source_handles": ["S1", "S2"]}])
    _, effects = apply_turn(state, plan)
    assert effects.clarification is None


def test_clarification_is_stored_and_answering_it_restores_its_context():
    state = ConversationState()
    ask = TurnPlan.build(task_type="clarify", turn_relation="new_question", metric="mean",
                         conditions={"city": "רמת גן", "year_from": 2024},
                         clarification={"key": "data_kind", "question": "לאיזה נתון הכוונה?",
                                        "options": DATA_KIND_OPTIONS})
    asked, effects = apply_turn(state, ask, question="מה המחיר הממוצע ברמת גן ב-2024?")
    assert effects.clarification.key == "data_kind" and asked.pending.key == "data_kind"
    assert asked.pending.original_question == "מה המחיר הממוצע ברמת גן ב-2024?"

    # An unrelated new question keeps the pending clarification (AE5) ...
    other = TurnPlan.build(task_type="locate", turn_relation="new_question", search_queries=["היתר בנייה"],
                           steps=[{"tool": "locate", "attribute_handle": None, "source_handles": []}])
    moved, _ = apply_turn(asked, other, question="אילו שומות מזכירות היתר בנייה?")
    assert moved.pending == asked.pending and moved.conditions == StateConditions()

    # ... and answering it later resolves it against the context it was asked in.
    answer = TurnPlan.build(task_type="compute", turn_relation="answer_to_clarification",
                            clarification_answer="transaction_price")
    done, effects = apply_turn(moved, answer, question="התכוונתי לעסקאות")
    assert effects.resolved == ("data_kind", "transaction_price")
    assert done.pending is None and done.metric == "mean"
    assert (done.conditions.data_kind, done.conditions.city, done.conditions.year_from) == (
        "transaction_price", "רמת גן", 2024)


def test_change_to_a_pending_clarification_keeps_it_open():
    asked, _ = apply_turn(ConversationState(), TurnPlan.build(
        task_type="clarify", conditions={"city": "רמת גן"},
        clarification={"key": "data_kind", "question": "לאיזה נתון הכוונה?", "options": DATA_KIND_OPTIONS}))
    changed, effects = apply_turn(asked, TurnPlan.build(task_type="clarify", turn_relation="change_clarification",
                                                        conditions={"city": "גבעתיים"}))
    assert changed.pending is not None and changed.pending.conditions.city == "גבעתיים"
    assert effects.resolved is None


def test_meta_turns_change_nothing():
    state = monetary_state()
    plan = TurnPlan.build(task_type="answer", turn_relation="meta_why",
                          steps=[{"tool": "explain_previous", "attribute_handle": None, "source_handles": []}])
    new, effects = apply_turn(state, plan, question="למה?")
    assert new.model_dump(exclude={"version", "recent_questions"}) == state.model_dump(
        exclude={"version", "recent_questions"})
    assert effects.changed == () and effects.cleared == ()


def test_state_keeps_only_the_last_three_questions_and_no_content():
    state = ConversationState()
    for q in ("ש1", "ש2", "ש3", "ש4"):
        state, _ = apply_turn(state, TurnPlan.build(), question=q)
    assert state.recent_questions == ["ש2", "ש3", "ש4"]
    assert set(state.model_dump()) == {
        "version", "task_type", "topic", "entities", "conditions", "attribute", "metric", "unit", "value_filter",
        "sources", "pending", "recent_questions"}
    assert set(SourceRef.model_fields) == {"document_id", "version_id", "page"}
    assert "text" not in PendingClarification.model_fields


def test_remember_sources_reuses_handles():
    a = SourceRef(document_id="d1", version_id="v1", page=1)
    b = SourceRef(document_id="d2", version_id="v2", page=None)
    state, handles = remember_sources(ConversationState(), [a, b])
    assert handles == ["S1", "S2"]
    state, handles = remember_sources(state, [b, SourceRef(document_id="d3", version_id="v3", page=4)])
    assert handles == ["S2", "S3"] and set(state.sources) == {"S1", "S2", "S3"}


def test_state_converts_to_query_conditions():
    q = monetary_state().query_conditions("calculation")
    assert (q.data_kind, q.year_from, q.neighborhood, q.intent) == ("transaction_price", 2023, "חרוזים", "calculation")
    assert monetary_state().monetary and not ConversationState(task_type="compute").monetary
