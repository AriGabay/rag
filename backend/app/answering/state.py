"""Typed conversation state and the pure function that applies a turn to it (KTD12, R17, R18, R20).

The state holds only structure: task type, topic, entities (surface text), conditions, the attribute
reference, metric and unit, a condition on the attribute's value, referenced sources as ``S#`` handles (document, version, page), the pending
clarification and the user's last three question texts. Answer text and document text never enter it, so
a later prompt cannot carry content the user can no longer see; handles are re-authorized every turn.

``apply_turn(state, plan)`` is pure: the same plan applied to the same starting state always gives the same
result, so a resubmitted turn (applied again to the state version it started from) does not apply twice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.answering.conditions import AreaType, DataKind, DateField, Intent, QueryConditions
from app.answering.plan import (
    CONDITION_KEYS,
    AttributeRef,
    ClarifyKey,
    ClarifyOption,
    ConditionDelta,
    Metric,
    PropertyType,
    ProposedClarification,
    TaskType,
    TurnPlan,
    ValueFilterSpec,
    VatBasis,
)

MAX_RECENT_QUESTIONS = 3
MAX_SOURCES = 30
COMPUTE_TASKS = ("compute", "compute_explain")
# Conditions that describe the basis of price records; they do not carry over to another attribute.
RECORD_BASIS_KEYS = ("data_kind", "date_field", "area_type", "vat_basis")

REFERENT_COMPARE_SECOND = "לא ברור לאיזו שומה שנייה הכוונה. עם איזו שומה להשוות?"
REFERENT_COMPARE_SIDES = "לא ברור בין אילו מסמכים להשוות. אילו שומות להשוות?"
REFERENT_YEAR = "לא ברור ביחס לאיזו שנה. לאיזו שנה הכוונה?"


class StateConditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str | None = None
    neighborhood: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    date_field: DateField | None = None
    data_kind: DataKind | None = None
    property_type: PropertyType | None = None
    area_type: AreaType | None = None
    vat_basis: VatBasis | None = None


class SourceRef(BaseModel):
    """What an ``S#`` handle points to. Re-authorized against current permissions on every turn."""

    model_config = ConfigDict(extra="forbid")

    document_id: str
    version_id: str
    page: int | None = None


class PendingClarification(BaseModel):
    """An open clarification and the structural context it was asked in (restored when it is answered)."""

    model_config = ConfigDict(extra="forbid")

    key: ClarifyKey
    question: str
    options: list[ClarifyOption] = Field(default_factory=list)
    original_question: str | None = None
    task_type: TaskType | None = None
    topic: str | None = None
    entities: list[str] = Field(default_factory=list)
    conditions: StateConditions = Field(default_factory=StateConditions)
    attribute: AttributeRef | None = None
    metric: Metric | None = None
    unit: str | None = None
    value_filter: ValueFilterSpec | None = None
    source_handles: list[str] = Field(default_factory=list)  # a comparison's sides already known (referent)
    search_queries: list[str] = Field(default_factory=list)  # the interrupted task's queries (resumed as they were)

    def proposal(self) -> ProposedClarification:
        return ProposedClarification(key=self.key, question=self.question, options=self.options)


class ConversationState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 0
    task_type: TaskType | None = None
    topic: str | None = None
    entities: list[str] = Field(default_factory=list)
    conditions: StateConditions = Field(default_factory=StateConditions)
    attribute: AttributeRef | None = None
    metric: Metric | None = None
    unit: str | None = None
    value_filter: ValueFilterSpec | None = None
    sources: dict[str, SourceRef] = Field(default_factory=dict)
    pending: PendingClarification | None = None
    recent_questions: list[str] = Field(default_factory=list)

    @property
    def monetary(self) -> bool:
        """The last turn computed over price records (a follow-up form then stays a money question)."""
        return self.task_type in COMPUTE_TASKS and self.conditions.data_kind is not None

    def query_conditions(self, intent: Intent = "calculation") -> QueryConditions:
        return QueryConditions.model_validate(self.conditions.model_dump() | {"intent": intent})

    def prompt_view(self) -> dict:
        """Structural fields for the interpreter prompt: handles only, never what they point to."""
        return {
            "task_type": self.task_type, "topic": self.topic, "entities": self.entities,
            "conditions": self.conditions.model_dump(exclude_none=True),
            "attribute": self.attribute.model_dump() if self.attribute else None,
            "metric": self.metric, "unit": self.unit,
            "value_filter": self.value_filter.model_dump() if self.value_filter else None,
            "source_handles": sorted(self.sources, key=_handle_no),
        }


@dataclass(frozen=True)
class TurnEffects:
    changed: tuple[str, ...]  # condition keys this turn set to a new value
    cleared: tuple[str, ...]  # keys this turn removed (stated, or dependents of a topic/attribute change)
    clarification: ProposedClarification | None  # ask this instead of executing the plan
    resolved: tuple[str, str] | None  # (key, value) of the pending clarification this turn answered
    topic_changed: bool
    base_version: int  # the state version the turn was applied to


@dataclass
class _Context:
    task_type: TaskType | None
    topic: str | None
    entities: list[str]
    conditions: dict[str, Any]
    attribute: AttributeRef | None
    metric: Metric | None
    unit: str | None
    value_filter: ValueFilterSpec | None = None

    @classmethod
    def of(cls, src: ConversationState | PendingClarification) -> _Context:
        return cls(src.task_type, src.topic, list(src.entities), src.conditions.model_dump(), src.attribute,
                   src.metric, src.unit, src.value_filter)


def _handle_no(handle: str) -> int:
    return int(handle[1:]) if handle[1:].isdigit() else 0


def _same_attribute(a: AttributeRef | None, b: AttributeRef | None) -> bool:
    if a is None or b is None:
        return a is b
    if a.handle or b.handle:
        return a.handle == b.handle
    return (a.description or "").strip() == (b.description or "").strip()


def _merge(conds: dict[str, Any], delta: ConditionDelta) -> bool:
    """Apply a condition delta in place. Returns False when a relative year has nothing to resolve against."""
    for key in delta.clear:
        for k in (("year_from", "year_to") if key == "years" else (key,)):
            conds[k] = None
    resolved = True
    if delta.relative_year_offset is not None:
        if conds["year_from"] is None:
            resolved = False
        else:
            conds["year_from"] += delta.relative_year_offset
            if conds["year_to"] is not None:
                conds["year_to"] += delta.relative_year_offset
    if delta.year_from is not None:
        conds["year_from"], conds["year_to"] = delta.year_from, delta.year_to
    elif delta.year_to is not None:
        conds["year_to"] = delta.year_to
    if delta.city is not None and delta.city != conds["city"]:
        conds["city"] = delta.city
        if delta.neighborhood is None:
            conds["neighborhood"] = None  # the old neighborhood belongs to the old city
    for key in ("neighborhood", "date_field", "data_kind", "property_type", "area_type", "vat_basis"):
        value = getattr(delta, key)
        if value is not None:
            conds[key] = value
    return resolved


def _referent_problem(plan: TurnPlan, state: ConversationState,
                      entities: list[str]) -> ProposedClarification | None:
    """A compare step must identify both sides; otherwise ask instead of comparing one side (R10, R18). Named
    entities (addresses, titles) may supply the sides: the orchestrator resolves them to documents and asks
    only when they do not give two sides."""
    for step in plan.steps:
        if step.tool != "compare":
            continue
        sides = list(dict.fromkeys(step.source_handles))
        if entities and len(sides) < 2:
            continue
        if len(sides) == 1:
            others = [h for h in sorted(state.sources, key=_handle_no) if h not in sides]
            return ProposedClarification(key="referent", question=REFERENT_COMPARE_SECOND,
                                         options=[ClarifyOption(value=h, label=f"מקור {h}") for h in others])
        if not sides:
            return ProposedClarification(key="referent", question=REFERENT_COMPARE_SIDES, options=[])
    return None


def _compare_sides(plan: TurnPlan) -> list[str]:
    return next((list(dict.fromkeys(s.source_handles)) for s in plan.steps if s.tool == "compare"), [])


def apply_turn(state: ConversationState, plan: TurnPlan, *,
               question: str | None = None) -> tuple[ConversationState, TurnEffects]:
    """Apply a validated plan to the state the turn started from (pure; KTD12)."""
    relation = plan.turn_relation
    recent = (state.recent_questions + [question])[-MAX_RECENT_QUESTIONS:] if question else state.recent_questions
    if relation in ("meta_why", "meta_sources"):
        new = state.model_copy(update={"version": state.version + 1, "recent_questions": list(recent)}, deep=True)
        return new, TurnEffects((), (), None, None, False, state.version)

    pending = state.pending
    resolved: tuple[str, str] | None = None
    topic_changed = False
    if relation in ("answer_to_clarification", "change_clarification") and pending is not None:
        ctx = _Context.of(pending)
    else:
        ctx = _Context.of(state)
    before = dict(ctx.conditions)
    before_attr, before_metric, before_filter = ctx.attribute, ctx.metric, ctx.value_filter

    if relation in ("new_question", "topic_change"):
        topic_changed = relation == "topic_change" or bool(state.topic and plan.topic and state.topic != plan.topic)
        ctx = _Context(None, None, [], {k: None for k in CONDITION_KEYS}, None, None, None, None)
    if relation in ("answer_to_clarification", "change_clarification") and pending is not None \
            and plan.clarification_answer is not None:
        if pending.key in CONDITION_KEYS:
            ctx.conditions[pending.key] = plan.clarification_answer
        elif pending.key == "attribute":
            ctx.attribute = AttributeRef(handle=plan.clarification_answer, description=None, unit_dimension=None)
        resolved = (pending.key, plan.clarification_answer)
        pending = None

    attribute_changed = plan.attribute is not None and not _same_attribute(plan.attribute, ctx.attribute)
    if attribute_changed and relation not in ("new_question", "topic_change"):
        for key in RECORD_BASIS_KEYS:
            ctx.conditions[key] = None
        ctx.metric, ctx.unit, ctx.value_filter = None, None, None
    year_resolved = _merge(ctx.conditions, plan.conditions)

    ctx.task_type = plan.task_type
    ctx.topic = plan.topic or ctx.topic
    ctx.entities = list(plan.entities) or ctx.entities
    ctx.attribute = plan.attribute if plan.attribute is not None else ctx.attribute
    ctx.metric = plan.metric if plan.metric != "none" else ctx.metric
    ctx.unit = plan.unit or ctx.unit
    ctx.value_filter = plan.value_filter or ctx.value_filter

    clarification = plan.clarification
    if clarification is None and not year_resolved:
        clarification = ProposedClarification(key="referent", question=REFERENT_YEAR, options=[])
    if clarification is None:
        clarification = _referent_problem(plan, state, ctx.entities)
    conditions = StateConditions.model_validate(ctx.conditions)
    if clarification is not None:
        pending = PendingClarification(
            key=clarification.key, question=clarification.question, options=clarification.options,
            original_question=question if resolved is None else (state.pending.original_question if state.pending
                                                                  else question),
            task_type=None if plan.task_type == "clarify" else plan.task_type, topic=ctx.topic,
            entities=ctx.entities, conditions=conditions, attribute=ctx.attribute, metric=ctx.metric, unit=ctx.unit,
            value_filter=ctx.value_filter, source_handles=_compare_sides(plan) if clarification.key == "referent"
            else [], search_queries=list(plan.search_queries))
        ctx.task_type = "clarify"
    elif relation == "change_clarification" and pending is not None:
        pending = pending.model_copy(update={"topic": ctx.topic, "entities": ctx.entities, "conditions": conditions,
                                             "attribute": ctx.attribute, "metric": ctx.metric, "unit": ctx.unit,
                                             "value_filter": ctx.value_filter})
        clarification = pending.proposal()  # re-ask in the changed context
        ctx.task_type = "clarify"

    after = conditions.model_dump()
    changed = tuple(k for k in CONDITION_KEYS if after[k] is not None and after[k] != before[k])
    cleared = [k for k in CONDITION_KEYS if before[k] is not None and after[k] is None]
    if before_attr is not None and (ctx.attribute is None or attribute_changed):
        cleared.append("attribute")
    if before_metric is not None and ctx.metric is None:
        cleared.append("metric")
    if before_filter is not None and ctx.value_filter is None:
        cleared.append("value_filter")
    new = ConversationState(
        version=state.version + 1, task_type=ctx.task_type, topic=ctx.topic, entities=ctx.entities,
        conditions=conditions, attribute=ctx.attribute, metric=ctx.metric, unit=ctx.unit,
        value_filter=ctx.value_filter, sources=dict(state.sources), pending=pending, recent_questions=list(recent))
    return new, TurnEffects(changed, tuple(cleared), clarification, resolved, topic_changed, state.version)


def remember_sources(state: ConversationState, refs: list[SourceRef]) -> tuple[ConversationState, list[str]]:
    """Give each cited source an ``S#`` handle, reusing the handle of an identical earlier reference."""
    sources = dict(state.sources)
    by_ref = {(r.document_id, r.version_id, r.page): h for h, r in sources.items()}
    next_no = max((_handle_no(h) for h in sources), default=0) + 1
    handles: list[str] = []
    for ref in refs:
        key = (ref.document_id, ref.version_id, ref.page)
        if key not in by_ref:
            by_ref[key] = f"S{next_no}"
            sources[f"S{next_no}"] = ref
            next_no += 1
        handles.append(by_ref[key])
    for old in sorted(sources, key=_handle_no)[:max(0, len(sources) - MAX_SOURCES)]:
        if old not in handles:
            del sources[old]
    return state.model_copy(update={"sources": sources}), handles
