"""The validated task plan of one user turn (KTD1, R1, R4).

The model (or the rules fast path) returns one ``TurnPlan``: the task type and turn relation, a condition
delta, an attribute reference, a metric, standalone search queries, at most four steps from a closed tool
set, and an optional clarification. The schema is strict-mode compatible (every field required, nullable
via ``| None``, no extra fields, no defaults), so it can be sent to OpenAI as is. Cross-field rules are not
in the schema: ``validate_plan`` enforces them on the server.

A plan may reference only enums and handles the server issued: ``A#`` attribute handles from the registry
and ``S#`` source handles from the conversation state. It never carries SQL, table or column names, or
record or document ids.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from app.answering.conditions import AreaType, DataKind, DateField
from app.answering.parser import Gazetteer

TaskType = Literal["locate", "answer", "compare", "compute", "compute_explain", "clarify", "abstain"]
TurnRelation = Literal["new_question", "follow_up", "answer_to_clarification", "change_clarification",
                       "meta_why", "meta_sources", "topic_change"]
Metric = Literal["count", "sum", "mean", "weighted_mean", "median", "min", "max", "range", "values", "none"]
Tool = Literal["search", "compute_records", "extract_and_compute", "compare", "locate", "explain_previous",
               "show_sources"]
PropertyType = Literal["apartment", "garden_apartment", "penthouse", "duplex", "cottage", "house", "office",
                       "retail", "land"]
VatBasis = Literal["included", "excluded"]
ClearKey = Literal["city", "neighborhood", "years", "date_field", "data_kind", "property_type", "area_type",
                   "vat_basis"]
ClarifyKey = Literal["data_kind", "date_field", "area_type", "property_type", "vat_basis", "referent", "attribute",
                     "place", "scope"]

MAX_STEPS = 4
MAX_QUERIES = 3
MAX_RELATIVE_YEARS = 50
CONDITION_KEYS = ("city", "neighborhood", "year_from", "year_to", "date_field", "data_kind", "property_type",
                  "area_type", "vat_basis")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ConditionDelta(_Strict):
    """What this turn states about the filters. ``None`` means "not stated"; ``clear`` removes a stored value."""

    city: str | None
    neighborhood: str | None
    year_from: int | None
    year_to: int | None
    relative_year_offset: int | None  # "השנה הקודמת" = -1, resolved by the server against stored conditions
    date_field: DateField | None
    data_kind: DataKind | None
    property_type: PropertyType | None
    area_type: AreaType | None
    vat_basis: VatBasis | None
    clear: list[ClearKey]


class AttributeRef(_Strict):
    """The attribute the user asks about: a registry handle, or the Hebrew surface text when none fits."""

    handle: str | None
    description: str | None
    unit_dimension: str | None


class Step(_Strict):
    tool: Tool
    attribute_handle: str | None
    source_handles: list[str]


class ClarifyOption(_Strict):
    value: str
    label: str


class ProposedClarification(_Strict):
    key: ClarifyKey
    question: str
    options: list[ClarifyOption]


class TurnPlan(_Strict):
    task_type: TaskType
    turn_relation: TurnRelation
    topic: str | None
    entities: list[str]
    conditions: ConditionDelta
    attribute: AttributeRef | None
    metric: Metric
    unit: str | None
    search_queries: list[str]
    steps: list[Step]
    clarification: ProposedClarification | None
    clarification_answer: str | None

    @classmethod
    def build(cls, **fields: Any) -> TurnPlan:
        """A server-made plan (rules, limited mode, tests): unspecified fields are empty."""
        conditions = {k: None for k in ConditionDelta.model_fields} | {"clear": []} | dict(fields.pop("conditions", {}))
        base: dict[str, Any] = {
            "task_type": "answer", "turn_relation": "new_question", "topic": None, "entities": [],
            "attribute": None, "metric": "none", "unit": None, "search_queries": [], "steps": [],
            "clarification": None, "clarification_answer": None,
        }
        return cls.model_validate(base | fields | {"conditions": conditions})


# Strings in a plan are data for tools: they must not look like SQL or carry raw ids.
_SQL = re.compile(
    r"(?i)\b(?:select|insert|update|delete|drop|alter|truncate|union|grant|revoke|create|exec(?:ute)?)\b\s+[\w*]"
    r"|--|/\*|\*/|\b(?:pg_catalog|pg_sleep|information_schema)\b|'\s*(?:or|and)\s+'?\w+'?\s*=")
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_PREFIXES = "בלמהו"


@dataclass
class PlanCheck:
    """A validated plan, or the reasons it was rejected (then the caller uses the limited path)."""

    plan: TurnPlan | None
    errors: list[str] = field(default_factory=list)
    unknown_place: str | None = None

    @property
    def ok(self) -> bool:
        return self.plan is not None and not self.errors


def _strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _known_place(name: str, known: Collection[str]) -> str | None:
    """The gazetteer spelling of ``name``, tolerating one leading Hebrew prefix letter ("ברמת גן")."""
    if name in known:
        return name
    if len(name) > 2 and name[0] in _PREFIXES and name[1:] in known:
        return name[1:]
    return None


def _years_ok(c: ConditionDelta) -> bool:
    for year in (c.year_from, c.year_to):
        if year is not None and not 1950 <= year <= 2100:
            return False
    if c.year_from is not None and c.year_to is not None and c.year_to < c.year_from:
        return False
    if c.relative_year_offset is not None and (
            c.year_from is not None or c.year_to is not None or abs(c.relative_year_offset) > MAX_RELATIVE_YEARS):
        return False
    return True


def validate_plan(raw: TurnPlan | dict, *, gazetteer: Gazetteer, attributes: Iterable[dict],
                  source_handles: Iterable[str], pending_options: Collection[str] | None = None) -> PlanCheck:
    """Server-side validation of a plan (KTD1 step 3, R4).

    Rejects schema violations (unknown tools, extra fields), more than four steps or three queries, a search
    step without a query, SQL- or UUID-looking strings, handles the server did not issue, a clarification
    answer that is not a pending option, and inconsistent years. A place outside the office's gazetteer
    does not reject the plan: it turns it into an unknown-place abstention.
    """
    try:
        plan = raw if isinstance(raw, TurnPlan) else TurnPlan.model_validate(raw)
        plan = TurnPlan.model_validate(plan.model_dump())  # a copy, re-checked even when built in code
    except ValidationError:
        return PlanCheck(None, ["schema"])

    errors: list[str] = []
    if len(plan.steps) > MAX_STEPS:
        errors.append("too_many_steps")
    if len(plan.search_queries) > MAX_QUERIES:
        errors.append("too_many_queries")
    if any(s.tool == "search" for s in plan.steps) and not any(q.strip() for q in plan.search_queries):
        errors.append("missing_query")
    strings = list(_strings(plan.model_dump()))
    if any(_SQL.search(s) for s in strings):
        errors.append("sql_like")
    if any(_UUID.search(s) for s in strings):
        errors.append("uuid_like")
    attr_handles = {a["handle"] for a in attributes}
    used_attrs = {s.attribute_handle for s in plan.steps} | {plan.attribute.handle if plan.attribute else None}
    if used_attrs - {None} - attr_handles:
        errors.append("unknown_attribute_handle")
    if {h for s in plan.steps for h in s.source_handles} - set(source_handles):
        errors.append("unknown_source_handle")
    if plan.clarification_answer is not None and (
            pending_options is None or plan.clarification_answer not in pending_options):
        errors.append("clarification_answer_not_an_option")
    if not _years_ok(plan.conditions):
        errors.append("invalid_years")
    if plan.task_type == "clarify" and (plan.clarification is None or not plan.clarification.question.strip()):
        errors.append("clarify_without_question")
    if errors:
        return PlanCheck(None, errors)

    cities = set(gazetteer.cities)
    hoods = {n for _, n in gazetteer.neighborhoods}
    c = plan.conditions
    city = _known_place(c.city, cities) if c.city else None
    hood = _known_place(c.neighborhood, hoods) if c.neighborhood else None
    unknown = (c.city if c.city and city is None else None) or (c.neighborhood if c.neighborhood and hood is None
                                                                 else None)
    if unknown:
        return PlanCheck(plan.model_copy(update={"task_type": "abstain", "steps": [], "clarification": None}),
                         [], unknown)
    if (city, hood) != (c.city, c.neighborhood):
        plan = plan.model_copy(update={"conditions": c.model_copy(update={"city": city, "neighborhood": hood})})
    return PlanCheck(plan)


# --- server policy over model plans (R3, R20, KTD1) -------------------------------------------------------

META_TOOLS = ("explain_previous", "show_sources")
COMPUTE_STEP_TOOLS = ("compute_records", "extract_and_compute")
# Clarifications the server cannot otherwise settle: which documents or which attribute the user means.
# Monetary keys (data kind, date field, bases) are asked by the computation itself, and only when the
# authorized records actually differ on them; any other key ("scope", "place" ...) is answered with
# stated coverage instead of a question.
MODEL_CLARIFY_KEYS = ("referent", "attribute")


def normalize_model_plan(plan: TurnPlan) -> TurnPlan:
    """The server's reading of a validated model plan: the model proposes, the server decides.

    - Meta tools run only on meta turns ("למה?", "תראה לי את המקור").
    - A proposed clarification is kept only for an unclear referent, or an attribute the plan does not
      name; otherwise the turn proceeds with what the plan already supports.
    - "abstain" with a named attribute becomes an answer, or a computation when a metric is asked:
      whether the repository holds the datum is checked by the tools, not guessed by the model.
    - A computation always carries a computation step."""
    meta = plan.turn_relation in ("meta_why", "meta_sources")
    steps = [s for s in plan.steps if meta or s.tool not in META_TOOLS]
    named = plan.attribute is not None and bool(plan.attribute.handle or plan.attribute.description)
    computes = named and plan.metric != "none"
    task = plan.task_type
    clarification = plan.clarification
    executable = bool(steps or plan.search_queries or named)
    if clarification is not None or task == "clarify":
        keep = not executable or (clarification is not None and (
            clarification.key == "referent" or (clarification.key == "attribute" and not named)))
        if not keep:
            clarification = None
            if task == "clarify":
                task = ("compute" if computes else
                        "compare" if any(s.tool == "compare" for s in steps) else "answer")
    if task == "abstain" and named:
        task = "compute" if computes else "answer"
    if task in ("compute", "compute_explain") and computes and not any(s.tool in COMPUTE_STEP_TOOLS for s in steps):
        steps = [Step(tool="extract_and_compute", attribute_handle=plan.attribute.handle, source_handles=[]),
                 *steps][:MAX_STEPS]
    if task in ("compute", "compute_explain") and not computes and not steps:
        task = "answer"
    return plan.model_copy(update={"task_type": task, "steps": steps, "clarification": clarification})
