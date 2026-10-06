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
    (plan_dict(steps=[{"tool": "search", "attribute_handle": None, "source_handles": []}] * (MAX_STEPS + 1),
               search_queries=["x"]), "too_many_steps"),
    (plan_dict(search_queries=["א", "ב", "ג", "ד"]), "too_many_queries"),
    (plan_dict(search_queries=["מחיר; DROP TABLE documents"]), "sql_like"),
    (plan_dict(search_queries=["SELECT * FROM transactions"]), "sql_like"),
    (plan_dict(entities=["3f2b1c9e-8a7d-4e6f-9b0a-1c2d3e4f5a6b"]), "uuid_like"),
    (plan_dict(attribute={"handle": "A9", "description": None, "unit_dimension": None}), "unknown_attribute_handle"),
    (plan_dict(steps=[{"tool": "compare", "attribute_handle": None, "source_handles": ["S1", "S7"]}]),
     "unknown_source_handle"),
    (plan_dict(clarification_answer="transaction_price"), "clarification_answer_not_an_option"),
    (plan_dict(conditions={"year_from": 2024, "year_to": 2022}), "invalid_years"),
    (plan_dict(conditions={"year_from": 2024, "relative_year_offset": -1}), "invalid_years"),
    (plan_dict(task_type="clarify"), "clarify_without_question"),
])
def test_invalid_plans_are_rejected(raw, error):
    result = check(raw)
    assert not result.ok and result.plan is None
    assert error in result.errors


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
    assert check(raw, pending_options=["transaction_price", "appraised_value"]).ok
    assert not check(raw, pending_options=["appraised_value"]).ok


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
