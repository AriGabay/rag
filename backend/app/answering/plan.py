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
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from app.answering.attributes import distinctive_words
from app.answering.conditions import AreaType, DataKind, DateField
from app.answering.parser import Gazetteer
from app.extraction.normalize_text import base_normalize, prefix_variants

if TYPE_CHECKING:
    from app.answering.state import ConversationState

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
ValueType = Literal["numeric", "text", "boolean", "date"]
FilterOp = Literal["<", "<=", ">", ">=", "=", "!="]

MAX_STEPS = 4
MAX_QUERIES = 3
MAX_RELATIVE_YEARS = 50
CONDITION_KEYS = ("city", "neighborhood", "year_from", "year_to", "date_field", "data_kind", "property_type",
                  "area_type", "vat_basis")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _with_absent(data: Any, *keys: str) -> Any:
    """Plans and states stored before a nullable field existed read it as None (the schema stays strict)."""
    if isinstance(data, dict) and any(k not in data for k in keys):
        data = {**{k: None for k in keys}, **data}
    return data


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
    """The attribute the user asks about: a registry handle, or the Hebrew surface text when none fits.
    ``value_type``: numeric (a quantity), text (a designation, a status, a description), boolean or date."""

    handle: str | None
    description: str | None
    unit_dimension: str | None
    value_type: ValueType | None

    @model_validator(mode="before")
    @classmethod
    def _older(cls, data: Any) -> Any:
        return _with_absent(data, "value_type")


class ValueFilterSpec(_Strict):
    """A condition on each case's value of the attribute ("smaller than 11", "= <a designation>"): a number in
    the attribute's unit, or a text value compared with = or != only."""

    op: FilterOp
    value: str


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
    value_filter: ValueFilterSpec | None

    @model_validator(mode="before")
    @classmethod
    def _older(cls, data: Any) -> Any:
        return _with_absent(data, "value_filter")

    @classmethod
    def build(cls, **fields: Any) -> TurnPlan:
        """A server-made plan (rules, limited mode, tests): unspecified fields are empty."""
        conditions = {k: None for k in ConditionDelta.model_fields} | {"clear": []} | dict(fields.pop("conditions", {}))
        base: dict[str, Any] = {
            "task_type": "answer", "turn_relation": "new_question", "topic": None, "entities": [],
            "attribute": None, "metric": "none", "unit": None, "search_queries": [], "steps": [],
            "clarification": None, "clarification_answer": None, "value_filter": None,
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
                  source_handles: Iterable[str], pending_options: Collection[str] | None = None,
                  pending_labels: dict[str, str] | None = None) -> PlanCheck:
    """Server-side validation of a plan (KTD1 step 3, R4).

    Rejects schema violations (unknown tools, extra fields), SQL- or UUID-looking strings, a contradictory
    year range and a clarification without a question. What is safe to repair is normalized instead, so a
    usable plan never falls to the limited path over a detail (each repair only removes or narrows):

    - handles the server did not issue (attribute or source) are dropped, never used;
    - more than four steps keep the first four distinct ones, more than three queries the first three;
    - a relative year next to an explicit year keeps the explicit year;
    - a clarification answer that is not a pending option is read by its label, else dropped (the turn is
      then a new question, and the pending clarification stays open);
    - an empty value filter is dropped.

    A place outside the office's gazetteer does not reject the plan: it turns it into an unknown-place
    abstention, unless it is part of a named entity (a street of an address), where it is dropped.
    """
    try:
        plan = raw if isinstance(raw, TurnPlan) else TurnPlan.model_validate(raw)
        plan = TurnPlan.model_validate(plan.model_dump())  # a copy, re-checked even when built in code
    except ValidationError:
        return PlanCheck(None, ["schema"])

    errors: list[str] = []
    strings = list(_strings(plan.model_dump()))
    if any(_SQL.search(s) for s in strings):
        errors.append("sql_like")
    if any(_UUID.search(s) for s in strings):
        errors.append("uuid_like")
    attr_handles = {a["handle"] for a in attributes}
    # A handle the server never issued is dropped, never used: the description still names the attribute.
    plan = _drop_unknown_attribute_handles(plan, attr_handles)
    if plan.attribute is not None and not (plan.attribute.handle or plan.attribute.description):
        errors.append("unknown_attribute_handle")
    plan = _repair(plan, set(source_handles), pending_options, pending_labels or {})
    if not _years_ok(plan.conditions):
        errors.append("invalid_years")
    if plan.task_type == "clarify" and (plan.clarification is None or not plan.clarification.question.strip()):
        if plan.steps or plan.search_queries or plan.attribute is not None:
            plan = plan.model_copy(update={"task_type": "compute" if plan.metric != "none" and plan.attribute
                                           else "answer", "clarification": None})
        else:
            # nothing to execute and no question: the model found the request unclear without saying how; the
            # server asks its own short referent question instead of discarding the plan to limited mode
            key = plan.clarification.key if plan.clarification is not None else "referent"
            plan = plan.model_copy(update={"clarification": ProposedClarification(
                key=key, question=REFERENT_UNCLEAR, options=list(plan.clarification.options)
                if plan.clarification is not None else [])})
    if errors:
        return PlanCheck(None, errors)

    cities = set(gazetteer.cities)
    hoods = {n for _, n in gazetteer.neighborhoods}
    c = plan.conditions
    city = _known_place(c.city, cities) if c.city else None
    hood = _known_place(c.neighborhood, hoods) if c.neighborhood else None
    unknown = (c.city if c.city and city is None else None) or (c.neighborhood if c.neighborhood and hood is None
                                                                 else None)
    if unknown and any(unknown in e for e in plan.entities):
        unknown = None  # a street of a named address read as a place: dropped below, the address scopes the turn
    if unknown:
        return PlanCheck(plan.model_copy(update={"task_type": "abstain", "steps": [], "clarification": None}),
                         [], unknown)
    if (city, hood) != (c.city, c.neighborhood):
        plan = plan.model_copy(update={"conditions": c.model_copy(update={"city": city, "neighborhood": hood})})
    return PlanCheck(plan)


def _repair(plan: TurnPlan, sources: set[str], pending_options: Collection[str] | None,
            pending_labels: dict[str, str]) -> TurnPlan:
    """The safe normalizations of ``validate_plan``: each one removes or narrows, never adds."""
    steps: list[Step] = []
    for s in plan.steps:
        s = s.model_copy(update={"source_handles": [h for h in dict.fromkeys(s.source_handles) if h in sources]})
        if s not in steps:
            steps.append(s)
    update: dict[str, Any] = {"steps": steps[:MAX_STEPS], "search_queries": [
        q for q in dict.fromkeys(plan.search_queries) if q and q.strip()][:MAX_QUERIES]}
    c = plan.conditions
    if c.relative_year_offset is not None and (c.year_from is not None or c.year_to is not None):
        update["conditions"] = c.model_copy(update={"relative_year_offset": None})
    answer = plan.clarification_answer
    if answer is not None and (pending_options is None or answer not in pending_options):
        by_label = {" ".join(label.split()): value for value, label in pending_labels.items()}
        answer = by_label.get(" ".join(answer.split())) if pending_options is not None else None
        update["clarification_answer"] = answer
        if answer is None and plan.turn_relation == "answer_to_clarification":
            update["turn_relation"] = "new_question"
    if plan.value_filter is not None and not plan.value_filter.value.strip():
        update["value_filter"] = None
    return plan.model_copy(update=update)


def _drop_unknown_attribute_handles(plan: TurnPlan, known: set[str]) -> TurnPlan:
    steps = [s if s.attribute_handle is None or s.attribute_handle in known
             else s.model_copy(update={"attribute_handle": None}) for s in plan.steps]
    attribute = plan.attribute
    if attribute is not None and attribute.handle is not None and attribute.handle not in known:
        attribute = attribute.model_copy(update={"handle": None})
    return plan.model_copy(update={"steps": steps, "attribute": attribute})


# --- server policy over model plans (R3, R20, KTD1) -------------------------------------------------------

META_TOOLS = ("explain_previous", "show_sources")
COMPUTE_STEP_TOOLS = ("compute_records", "extract_and_compute")
# Clarifications the server cannot otherwise settle: which documents or which attribute the user means.
# Monetary keys (data kind, date field, bases) are asked by the computation itself, and only when the
# authorized records actually differ on them; any other key ("scope", "place" ...) is answered with
# stated coverage instead of a question.
MODEL_CLARIFY_KEYS = ("referent", "attribute")


def _same_ref(a: AttributeRef | None, b: AttributeRef | None) -> bool:
    if a is None or b is None:
        return a is b
    if a.handle or b.handle:
        return a.handle == b.handle
    return " ".join((a.description or "").split()) == " ".join((b.description or "").split())


def _states_condition(plan: TurnPlan) -> bool:
    c = plan.conditions
    return bool(c.clear) or any(getattr(c, k) is not None for k in (*CONDITION_KEYS, "relative_year_offset"))


def _relation(plan: TurnPlan, state: ConversationState | None) -> str:
    """A change to the pending clarification must keep that task's attribute and metric and change a
    condition ("ובגבעתיים?"); anything else is a new question, and the clarification stays open (R20). A
    "change" with nothing pending that names another attribute or topic than the conversation is a topic
    change; one that only states a condition of the conversation's question is a follow-up."""
    relation = plan.turn_relation
    if relation != "change_clarification":
        return relation
    pending = state.pending if state is not None else None
    if pending is not None:
        same_task = (plan.attribute is None or _same_ref(plan.attribute, pending.attribute)) and \
            plan.metric in ("none", pending.metric or "none")
        return relation if same_task and _states_condition(plan) else "new_question"
    if state is not None and ((plan.attribute is not None and state.attribute is not None
                               and not _same_ref(plan.attribute, state.attribute))
                              or (plan.topic and state.topic and plan.topic != state.topic)):
        return "topic_change"
    if state is not None and (state.attribute is not None or state.task_type) and _states_condition(plan):
        return "follow_up"  # "ובתל אביב?" with nothing pending: the same question, one condition changed
    return "new_question"


REFERENT_UNCLEAR = "לא ברור לי למה הפנייה מתייחסת. אפשר לכתוב את השאלה המלאה, עם הנושא, המסמך או הנכס?"
ORDERING_OPS = ("<", "<=", ">", ">=")
ARITHMETIC_METRICS = ("sum", "mean", "weighted_mean", "median", "min", "max", "range")


def _numeric_when_ordered(plan: TurnPlan) -> TurnPlan:
    """An ordering condition ("after 2010", "below 11") or arithmetic needs numbers: an attribute typed as a
    date, text or boolean is then computed as a number (a year compares as a number)."""
    ref = plan.attribute
    ordered = plan.value_filter is not None and plan.value_filter.op in ORDERING_OPS
    if ref is None or ref.value_type in (None, "numeric") or not (ordered or plan.metric in ARITHMETIC_METRICS):
        return plan
    return plan.model_copy(update={"attribute": ref.model_copy(update={"value_type": "numeric"})})


def _names_clearly(plan: TurnPlan) -> bool:
    """The plan names an attribute by a handle or by a description with a word that says what is measured;
    a description of measure words only ("הגודל הממוצע") leaves the attribute open."""
    ref = plan.attribute
    if ref is None or not (ref.handle or ref.description):
        return False
    return bool(distinctive_words(ref.description)) if ref.description else True


def _mentioned(place: str, question: str) -> bool:
    """Whether the question names the place (or the part of a hyphenated name before the hyphen, "תל אביב" for
    "תל אביב-יפו"), with or without a prefix letter."""
    tokens = [t.strip(".,:;?!()\"'") for t in base_normalize(question).split()]
    names = {base_normalize(place), base_normalize(place.split("-")[0])}
    for name in names:
        words = name.split()
        if not words:
            continue
        for i in range(len(tokens) - len(words) + 1):
            first = tokens[i]
            if (first == words[0] or words[0] in prefix_variants(first)
                    or (len(first) > 1 and first[0] in "בלמהושכ" and first[1:] == words[0])) and \
                    tokens[i + 1:i + len(words)] == words[1:]:
                return True
    return False


def _grounded_places(plan: TurnPlan, question: str | None) -> TurnPlan:
    """A place condition comes only from the turn's own words. The model may copy the conversation's city into
    a new question or a topic change, or add a city the question never names (a street it places in a city):
    such a place is dropped. A follow-up keeps the conversation's places through the state, not the plan."""
    if not question or plan.turn_relation in ("meta_why", "meta_sources", "answer_to_clarification"):
        return plan
    c = plan.conditions
    drop = {k: None for k in ("city", "neighborhood") if getattr(c, k) and not _mentioned(getattr(c, k), question)}
    if not drop:
        return plan
    return plan.model_copy(update={"conditions": c.model_copy(update=drop)})


def normalize_model_plan(plan: TurnPlan, state: ConversationState | None = None,
                         question: str | None = None) -> TurnPlan:
    """The server's reading of a validated model plan: the model proposes, the server decides. Every rule
    reads the plan's structure (task, attribute, metric, filter, steps), never its words.

    - Meta tools run only on meta turns ("למה?", "תראה לי את המקור").
    - A proposed clarification is kept only for an unclear referent, or an unclear attribute: one the plan
      does not name, or names only by measure words ("the average size"). A clearly named attribute
      proceeds even when the model chose to ask. A turn with nothing to execute keeps its clarification.
    - "abstain" with a named attribute becomes an answer, or a computation when a metric is asked:
      whether the repository holds the datum is checked by the tools, not guessed by the model.
    - A named attribute with a metric, or with a condition on its value, is a computation even when the
      model planned an answer; a planned document list becomes one only with a condition or an aggregate
      (not a bare "values"). A condition with no metric (or "values") counts the cases that meet it, and
      the answer lists them.
    - A follow-up that names no other attribute repeats the conversation's computation.
    - An attribute ordered or aggregated as a number is numeric, whatever type the model gave it.
    - A computation always carries a computation step.
    - A change to a pending clarification is accepted only as described in ``_relation``.
    - A place condition the question does not name is dropped (``_grounded_places``)."""
    meta = plan.turn_relation in ("meta_why", "meta_sources")
    plan = _grounded_places(plan, question)
    relation = _relation(plan, state)
    plan = _numeric_when_ordered(plan)
    steps = [s for s in plan.steps if meta or s.tool not in META_TOOLS]
    named = plan.attribute is not None and bool(plan.attribute.handle or plan.attribute.description)
    metric = plan.metric
    if plan.value_filter is not None and named and metric in ("none", "values"):
        metric = "count"
    computes = named and metric != "none"
    task = plan.task_type
    if (relation == "follow_up" and state is not None and state.task_type in ("compute", "compute_explain")
            and task in ("answer", "locate") and not any(s.tool == "compare" for s in steps)
            and (plan.attribute is None or _same_ref(plan.attribute, state.attribute))):
        task = state.task_type  # "ומה לגבי X?" after a computation: the same computation, one condition changed
        computes = True
    clarification = plan.clarification
    executable = bool(steps or plan.search_queries or named)
    if clarification is not None or task == "clarify":
        keep = not executable or (clarification is not None and (
            clarification.key == "referent" or (clarification.key == "attribute" and not _names_clearly(plan))))
        if not keep:
            clarification = None
            if task == "clarify":
                task = ("compute" if computes else
                        "compare" if any(s.tool == "compare" for s in steps) else "answer")
    if task == "abstain" and named:
        task = "compute" if computes else "answer"
    # "which documents mention X" may come with metric "values": a document list unless a value is conditioned
    # or aggregated; a question about a value with a metric is a computation
    aggregated = plan.value_filter is not None or metric not in ("none", "values")
    if computes and not meta and (task == "answer" or (task == "locate" and aggregated)):
        task = "compute"
    if task in ("compute", "compute_explain") and computes and not any(s.tool in COMPUTE_STEP_TOOLS for s in steps):
        handle = plan.attribute.handle if plan.attribute is not None else None
        steps = [Step(tool="extract_and_compute", attribute_handle=handle, source_handles=[]), *steps][:MAX_STEPS]
    if task in ("compute", "compute_explain") and not computes and not steps:
        task = "answer"
    if task in ("compute", "compute_explain") and computes:
        steps = [s for s in steps if s.tool != "locate"]  # a computation is not presented as a document list
    return plan.model_copy(update={"task_type": task, "turn_relation": relation, "steps": steps, "metric": metric,
                                   "clarification": clarification})
