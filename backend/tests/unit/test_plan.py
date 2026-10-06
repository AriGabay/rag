"""TurnPlan schema and server-side validation (U4, KTD1, R1, R4)."""

from __future__ import annotations

import pytest
from openai.lib._pydantic import to_strict_json_schema

from app.answering.parser import Gazetteer
from app.answering.plan import MAX_STEPS, TurnPlan, validate_plan

GAZ = Gazetteer(cities=["רמת גן", "גבעתיים"], neighborhoods=[("רמת גן", "חרוזים")])
ATTRS = [
    {"handle": "A1", "key": "price_per_sqm", "label": "מחיר למ״ר", "aliases": [], "unit_dimension": "money_per_area",
     "source": "structured"},
    {"handle": "A2", "key": "safe_room_area", "label": "שטח ממ״ד", "aliases": ["ממ״ד"], "unit_dimension": "area",
     "source": "extracted"},
]


def plan_dict(**overrides) -> dict:
    return TurnPlan.build(**overrides).model_dump()


def check(raw, **kw):
    return validate_plan(raw, gazetteer=GAZ, attributes=ATTRS, source_handles=kw.pop("sources", ["S1", "S2"]), **kw)


def _objects(schema: dict):
    stack = [schema]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                yield node
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)


def test_schema_is_strict_mode_compatible():
    schema = to_strict_json_schema(TurnPlan)
    objects = list(_objects(schema))
    assert objects
    for obj in objects:
        assert obj.get("additionalProperties") is False
        assert sorted(obj["required"]) == sorted(obj["properties"])
    text = str(schema)
    assert "allOf" not in text and "'default'" not in text


def test_valid_plan_passes():
    raw = plan_dict(task_type="compute", attribute={"handle": "A2", "description": "שטח ממ״ד", "unit_dimension": "area"},
                    metric="mean", conditions={"city": "רמת גן"}, search_queries=["שטח ממ״ד ברמת גן"],
                    steps=[{"tool": "extract_and_compute", "attribute_handle": "A2", "source_handles": []}])
    result = check(raw)
    assert result.ok and result.errors == [] and result.plan.conditions.city == "רמת גן"


@pytest.mark.parametrize("raw, error", [
    (plan_dict(search_queries=["מחיר; DROP TABLE documents"]), "sql_like"),
    (plan_dict(search_queries=["SELECT * FROM transactions"]), "sql_like"),
    (plan_dict(entities=["3f2b1c9e-8a7d-4e6f-9b0a-1c2d3e4f5a6b"]), "uuid_like"),
    (plan_dict(attribute={"handle": "A9", "description": None, "unit_dimension": None,
                          "value_type": None}), "unknown_attribute_handle"),
    (plan_dict(conditions={"year_from": 2024, "year_to": 2022}), "invalid_years"),
    (plan_dict(task_type="clarify"), "clarify_without_question"),
])
def test_invalid_plans_are_rejected(raw, error):
    result = check(raw)
    assert not result.ok and result.plan is None
    assert error in result.errors


def test_safe_details_are_repaired_instead_of_rejected():
    """A usable plan never falls to the limited path over a detail; each repair only removes or narrows."""
    search = {"tool": "search", "attribute_handle": None, "source_handles": []}
    many = check(plan_dict(steps=[search] * (MAX_STEPS + 1) + [{**search, "tool": "locate"}] * 4,
                           search_queries=["א", "ב", "ג", "ד"]))
    assert many.ok and [s.tool for s in many.plan.steps] == ["search", "locate"]
    assert many.plan.search_queries == ["א", "ב", "ג"]
    handles = check(plan_dict(task_type="compare", steps=[{"tool": "compare", "attribute_handle": None,
                                                           "source_handles": ["S1", "S7", "S1"]}]))
    assert handles.ok and handles.plan.steps[0].source_handles == ["S1"]  # S7 was never issued: dropped
    fresh = check(plan_dict(task_type="compare", steps=[{"tool": "compare", "attribute_handle": None,
                                                         "source_handles": ["S1", "S2"]}]), sources=[])
    assert fresh.ok and fresh.plan.steps[0].source_handles == []
    years = check(plan_dict(conditions={"year_from": 2024, "relative_year_offset": -1}))
    assert years.ok and years.plan.conditions.year_from == 2024 and years.plan.conditions.relative_year_offset is None
    stray = check(plan_dict(turn_relation="answer_to_clarification", clarification_answer="transaction_price"))
    assert stray.ok and stray.plan.clarification_answer is None and stray.plan.turn_relation == "new_question"
    empty = check(plan_dict(value_filter={"op": ">", "value": " "}))
    assert empty.ok and empty.plan.value_filter is None


def test_clarification_answer_by_label_is_read_as_its_option():
    raw = plan_dict(turn_relation="answer_to_clarification", clarification_answer="מחירי עסקאות")
    result = check(raw, pending_options=["transaction_price", "appraised_value"],
                   pending_labels={"transaction_price": "מחירי עסקאות", "appraised_value": "שווי נכסים נישומים"})
    assert result.ok and result.plan.clarification_answer == "transaction_price"
    assert result.plan.turn_relation == "answer_to_clarification"


def test_plans_stored_before_the_value_fields_still_load():
    stored = plan_dict()
    stored.pop("value_filter")
    stored["attribute"] = {"handle": None, "description": "גובה החלל", "unit_dimension": "length"}
    plan = TurnPlan.model_validate(stored)
    assert plan.value_filter is None and plan.attribute.value_type is None


def test_street_read_as_a_place_inside_a_named_address_does_not_abstain():
    result = check(plan_dict(conditions={"neighborhood": "הדקל"}, entities=["רחוב הדקל 4"]))
    assert result.ok and result.unknown_place is None and result.plan.task_type == "answer"
    assert result.plan.conditions.neighborhood is None


def test_unknown_tool_and_extra_fields_are_rejected():
    unknown_tool = plan_dict()
    unknown_tool["steps"] = [{"tool": "run_sql", "attribute_handle": None, "source_handles": []}]
    extra = plan_dict()
    extra["sql"] = "SELECT 1"
    extra_nested = plan_dict()
    extra_nested["conditions"]["table"] = "transactions"
    for raw in (unknown_tool, extra, extra_nested):
        result = check(raw)
        assert not result.ok and result.errors == ["schema"]


def test_place_outside_gazetteer_becomes_unknown_place_abstention():
    raw = plan_dict(task_type="compute", conditions={"city": "חיפה"}, metric="mean", search_queries=["מחיר בחיפה"],
                    steps=[{"tool": "compute_records", "attribute_handle": None, "source_handles": []}])
    result = check(raw)
    assert result.ok and result.unknown_place == "חיפה"
    assert result.plan.task_type == "abstain" and result.plan.steps == []


def test_prefixed_gazetteer_place_is_normalized():
    result = check(plan_dict(conditions={"city": "ברמת גן", "neighborhood": "בחרוזים"}))
    assert result.ok and result.unknown_place is None
    assert (result.plan.conditions.city, result.plan.conditions.neighborhood) == ("רמת גן", "חרוזים")


def test_clarification_answer_must_be_a_pending_option():
    raw = plan_dict(turn_relation="answer_to_clarification", clarification_answer="transaction_price")
    assert check(raw, pending_options=["transaction_price", "appraised_value"]).plan.clarification_answer \
        == "transaction_price"
    other = check(raw, pending_options=["appraised_value"])  # not an option: never applied, a new question
    assert other.ok and other.plan.clarification_answer is None and other.plan.turn_relation == "new_question"


# --- server policy over model plans -------------------------------------------------------------------------

def _model_plan(**kw):
    from app.answering.plan import AttributeRef, ClarifyOption, ProposedClarification, Step, TurnPlan

    clar = kw.pop("clarification", None)
    if clar:
        clar = ProposedClarification(key=clar, question="?", options=[ClarifyOption(value="a", label="א"),
                                                                       ClarifyOption(value="b", label="ב")])
    attr = kw.pop("attribute", None)
    attr = AttributeRef(handle=None, description=attr, unit_dimension="area") if attr else None
    steps = [Step(tool=t, attribute_handle=None, source_handles=[]) for t in kw.pop("tools", [])]
    return TurnPlan.build(clarification=clar, attribute=attr, steps=steps, **kw)


def test_scope_clarification_is_dropped_and_the_computation_runs():
    from app.answering.plan import normalize_model_plan

    p = normalize_model_plan(_model_plan(task_type="clarify", clarification="scope", attribute="גודל החלל",
                                         metric="mean", tools=["search"]))
    assert p.clarification is None and p.task_type == "compute"
    assert [s.tool for s in p.steps][0] == "extract_and_compute"


def test_referent_clarification_is_kept():
    from app.answering.plan import normalize_model_plan

    p = normalize_model_plan(_model_plan(task_type="clarify", clarification="referent", tools=["compare"]))
    assert p.task_type == "clarify" and p.clarification.key == "referent"


def test_monetary_clarification_from_the_model_is_left_to_the_computation():
    from app.answering.plan import normalize_model_plan

    p = normalize_model_plan(_model_plan(task_type="clarify", clarification="data_kind", attribute="מחיר",
                                         metric="mean", tools=["compute_records"]))
    assert p.clarification is None and p.task_type == "compute"


def test_abstain_with_a_named_attribute_becomes_an_answer_or_a_computation():
    from app.answering.plan import normalize_model_plan

    assert normalize_model_plan(_model_plan(task_type="abstain", attribute="גובה החלל")).task_type == "answer"
    computed = normalize_model_plan(_model_plan(task_type="abstain", attribute="גובה החלל", metric="mean"))
    assert computed.task_type == "compute" and computed.steps[0].tool == "extract_and_compute"


def test_abstain_without_anything_named_stays():
    from app.answering.plan import normalize_model_plan

    assert normalize_model_plan(_model_plan(task_type="abstain")).task_type == "abstain"


def test_meta_tools_are_stripped_from_new_questions_only():
    from app.answering.plan import normalize_model_plan

    p = normalize_model_plan(_model_plan(task_type="answer", tools=["search", "show_sources", "explain_previous"]))
    assert [s.tool for s in p.steps] == ["search"]
    m = normalize_model_plan(_model_plan(task_type="answer", turn_relation="meta_sources", tools=["show_sources"]))
    assert [s.tool for s in m.steps] == ["show_sources"]


def test_unexecutable_clarification_is_kept():
    from app.answering.plan import normalize_model_plan

    p = normalize_model_plan(_model_plan(task_type="clarify", clarification="scope"))
    assert p.task_type == "clarify" and p.clarification is not None


def test_search_step_without_queries_is_accepted_and_searches_the_question():
    """The orchestrator falls back to the question itself; an empty query list is not a reason to reject."""
    result = check(plan_dict(steps=[{"tool": "search", "attribute_handle": None, "source_handles": []}],
                             search_queries=[]))
    assert result.ok


def test_unknown_attribute_handles_are_dropped_not_used():
    result = check(plan_dict(attribute={"handle": "A9", "description": "גודל החלל", "unit_dimension": "area"},
                             steps=[{"tool": "extract_and_compute", "attribute_handle": "A9", "source_handles": []}],
                             search_queries=["x"]))
    assert result.ok
    assert result.plan.attribute.handle is None and result.plan.attribute.description == "גודל החלל"
    assert result.plan.steps[0].attribute_handle is None


def test_clarify_without_a_question_but_with_work_to_do_becomes_that_work():
    result = check(plan_dict(task_type="clarify", metric="mean", search_queries=["x"],
                             attribute={"handle": None, "description": "גודל החלל", "unit_dimension": "area"}))
    assert result.ok and result.plan.task_type == "compute" and result.plan.clarification is None


# --- routing by plan structure (compute vs answer/locate, follow-ups, clarification relation) ---------------

def test_named_attribute_with_a_metric_is_a_computation_even_when_planned_as_an_answer():
    from app.answering.plan import normalize_model_plan

    p = normalize_model_plan(_model_plan(task_type="answer", attribute="גובה החלל", metric="min", tools=["search"]))
    assert p.task_type == "compute" and [s.tool for s in p.steps] == ["extract_and_compute", "search"]


def test_condition_on_the_value_counts_the_cases_and_drops_the_document_list():
    from app.answering.plan import ValueFilterSpec, normalize_model_plan

    p = normalize_model_plan(_model_plan(task_type="locate", attribute="גובה החלל", tools=["locate", "search"],
                                         value_filter=ValueFilterSpec(op=">", value="2.7")))
    assert p.task_type == "compute" and p.metric == "count" and p.value_filter.value == "2.7"
    assert [s.tool for s in p.steps] == ["extract_and_compute", "search"]
    listed = normalize_model_plan(_model_plan(task_type="compute", attribute="גובה החלל", metric="values",
                                              value_filter=ValueFilterSpec(op="<", value="3")))
    assert listed.metric == "count"
    plain = normalize_model_plan(_model_plan(task_type="locate", tools=["locate"]))
    assert plain.task_type == "locate" and plain.metric == "none"


def test_follow_up_after_a_computation_repeats_it():
    from app.answering.plan import AttributeRef, normalize_model_plan
    from app.answering.state import ConversationState

    state = ConversationState(task_type="compute", metric="mean", attribute=AttributeRef(
        handle=None, description="גובה החלל", unit_dimension="length"))
    p = normalize_model_plan(_model_plan(task_type="answer", turn_relation="follow_up",
                                         conditions={"city": "גבעתיים"}, tools=["search"]), state)
    assert p.task_type == "compute" and p.steps[0].tool == "extract_and_compute"
    other = normalize_model_plan(_model_plan(task_type="answer", turn_relation="follow_up", attribute="שטח החלל",
                                             tools=["search"]), state)
    assert other.task_type == "answer"  # another attribute without a metric: a content question


def test_change_clarification_is_accepted_only_for_a_condition_change_of_the_same_task():
    from app.answering.plan import AttributeRef, normalize_model_plan
    from app.answering.state import ConversationState, PendingClarification

    priced = AttributeRef(handle="A1", description="מחיר למ״ר", unit_dimension="money_per_area")
    state = ConversationState(task_type="clarify", attribute=priced, pending=PendingClarification(
        key="data_kind", question="?", task_type="compute", attribute=priced, metric="mean"))
    new = normalize_model_plan(_model_plan(task_type="answer", turn_relation="change_clarification",
                                           attribute="שטח החלל", tools=["search"]), state)
    assert new.turn_relation == "new_question"
    same = normalize_model_plan(_model_plan(task_type="compute", turn_relation="change_clarification",
                                            conditions={"city": "גבעתיים"}), state)
    assert same.turn_relation == "change_clarification"
    unchanged = normalize_model_plan(_model_plan(task_type="compute", turn_relation="change_clarification"), state)
    assert unchanged.turn_relation == "new_question"  # changes no condition: not a change to the clarification
    nothing_pending = ConversationState(task_type="answer", topic="א", attribute=AttributeRef(
        handle=None, description="גובה החלל", unit_dimension="length"))
    moved = normalize_model_plan(_model_plan(task_type="answer", turn_relation="change_clarification",
                                             attribute="שטח החלל", tools=["search"]), nothing_pending)
    assert moved.turn_relation == "topic_change"


def test_an_ordered_or_aggregated_attribute_is_numeric():
    from app.answering.plan import AttributeRef, ValueFilterSpec, normalize_model_plan

    year = AttributeRef(handle=None, description="שנת האירוע", unit_dimension=None, value_type="date")
    after = normalize_model_plan(TurnPlan.build(task_type="compute", metric="count", attribute=year,
                                                value_filter=ValueFilterSpec(op=">", value="2010")))
    assert after.attribute.value_type == "numeric"
    oldest = normalize_model_plan(TurnPlan.build(task_type="compute", metric="min", attribute=year))
    assert oldest.attribute.value_type == "numeric"
    label = AttributeRef(handle=None, description="סיווג", unit_dimension=None, value_type="text")
    listed = normalize_model_plan(TurnPlan.build(task_type="compute", metric="values", attribute=label,
                                                 value_filter=ValueFilterSpec(op="=", value="א")))
    assert listed.attribute.value_type == "text" and listed.metric == "count"
    plain = normalize_model_plan(TurnPlan.build(task_type="locate", metric="values", attribute=label,
                                                steps=[{"tool": "locate", "attribute_handle": None,
                                                        "source_handles": []}]))
    assert plain.task_type == "locate"  # "which documents mention X": still a document list


def test_attribute_clarification_chosen_by_the_model_is_kept_over_a_generic_description():
    """GQ53: "the average size" names no attribute; the model asks which, even though it wrote "גודל"."""
    from app.answering.plan import normalize_model_plan

    asked = normalize_model_plan(_model_plan(task_type="clarify", clarification="attribute", attribute="גודל",
                                             metric="mean"))
    assert asked.task_type == "clarify" and asked.clarification.key == "attribute"
    side = normalize_model_plan(_model_plan(task_type="compute", clarification="attribute", attribute="גודל החלל",
                                            metric="mean"))
    assert side.clarification is None and side.task_type == "compute"
