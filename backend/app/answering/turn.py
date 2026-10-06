"""The turn orchestrator (U9; KTD1, KTD2, KTD12-KTD14; R1, R3, R17-R21, R25, R26).

One user turn, called by ``api.ask`` after it reserved the turn in a short transaction (``load`` runs there):

1. ``interpret_turn``, outside any transaction: a clarification button, an edit of the context chips, or
   free text through ``interpret`` (rules fast path; the model only in cloud mode; limited mode otherwise).
   Explicit condition edits (``filters``, ``remove``) join the plan's delta, and a relative year is resolved
   against the state the turn started from, so a retried turn can never apply it twice.
2. ``execute_turn``: ``apply_turn`` to the state, then the cache (KTD14), then at most four tool steps and
   at most one replan. Every tool opens its own short ``tenant_tx`` under the user's context; model calls
   (interpretation, extraction, answer, judge) never run inside a transaction.
3. ``api.ask`` completes the reserved row, sources, state and cache in one final transaction guarded by
   ``state_version``.

Results that are never cached: clarifications, meta-turns, partial or deadline results, fallbacks, turns
with a non-ok provider status, and error mode.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from decimal import Decimal
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import Connection, text

from app.answering import facts
from app.answering.attributes import AttributeDef, handle_map, list_attribute_handles, resolve_attribute
from app.answering.compare import CompareSide, compose_comparison, gather_sides
from app.answering.compose import (
    ABSTENTION_TEXT,
    CLAIMS_POLICY,
    COMPARE_POLICY,
    ComputedValue,
    answer_fields,
    compose_answer,
    computed_from_numeric,
    no_evidence_kind,
    source_json,
    sources_still_authorized,
)
from app.answering.conditions import DATE_FIELD_LABELS, QueryConditions
from app.answering.content import EVIDENCE_LIMIT, evidence_from_hits, log_usage, log_usages, select_provider
from app.answering.coverage import coverage
from app.answering.interpret import (
    INTERPRET_INSTRUCTIONS,
    LIMITED_MODE_NOTE,
    PROMPT_VERSION,
    RECORD_CLARIFY_KEYS,
    answer_plan,
    build_interpret_input,
    interpret,
)
from app.answering.metadata import MetadataFilters
from app.answering.parser import Gazetteer, missing_conditions
from app.answering.plan import (
    MAX_STEPS,
    ClarifyOption,
    ConditionDelta,
    ProposedClarification,
    Step,
    TurnPlan,
    normalize_model_plan,
)
from app.answering.plan import validate_plan as _validate_plan
from app.answering.service import (
    CLARIFY_QUESTIONS,
    clarification_answer,
    clarification_options,
    load_gazetteer,
    mixed_basis,
    numeric_answer,
    source_json_rows,
)
from app.answering.state import (
    REFERENT_COMPARE_SECOND,
    AttributeRef,
    ConversationState,
    PendingClarification,
    SourceRef,
    apply_turn,
    remember_sources,
)
from app.answering.templates import (
    OPERATION_LABELS,
    TEMPLATE_VERSION,
    UNIT_LABELS,
    abstain_text,
    number,
    structured_text,
)
from app.appraisal.query import compute_records, sources_for, uncertain_duplicates
from app.config import get_settings
from app.db import TenantContext, current_data_version, tenant_tx
from app.platform.documents import source_file_url
from app.platform.search import search_evidence
from app.providers.embeddings import get_embedding_provider
from app.providers.llm import SYSTEM_POLICY, CallStatus, LLMProvider, Purpose
from app.providers.status import FAILURE_REASONS, Mode, ProviderState

FILTER_CONFLICT = "filter_conflict"
EDIT_KEYS = ("city", "neighborhood", "year_from", "year_to", "date_field", "data_kind", "property_type", "area_type",
             "vat_basis")
# Chip keys a client may remove -> the delta's clear key (None: cleared on the state after the turn).
REMOVE_KEYS = {"city": "city", "neighborhood": "neighborhood", "years": "years", "year_from": "years",
               "year_to": "years", "date_field": "date_field", "data_kind": "data_kind",
               "property_type": "property_type", "area_type": "area_type", "vat_basis": "vat_basis",
               "attribute": None, "metric": None}
COMPUTE_TOOLS = ("compute_records", "extract_and_compute")
CONTENT_TOOLS = ("search", "locate", "compare")
META_RELATIONS = ("meta_why", "meta_sources")
MONETARY_DIMENSIONS = ("currency", "currency_per_area")
DEFAULT_ATTRIBUTE_LABEL = "מחיר למ״ר"
CACHEABLE_KINDS = ("numeric", "content", "combined", "abstain")
PER_TURN_FIELDS = ("interpretation_note", "cleared", "cached")
DEADLINE_MARGIN_SECONDS = 3.0
# Answers and judge prompts are part of the cache key: a prompt change never serves an old answer.
ANSWER_PROMPT_VERSION = PROMPT_VERSION + ":" + hashlib.sha256(
    (SYSTEM_POLICY + CLAIMS_POLICY + COMPARE_POLICY + INTERPRET_INSTRUCTIONS).encode()).hexdigest()[:10]

NO_PENDING = "אין שאלת הבהרה פתוחה בשיחה זו. כתבו שאלה חדשה"
NEED_QUESTION = "יש לכתוב שאלה"
STATE_CONFLICT = "השיחה עודכנה בבקשה אחרת באותו זמן. נסו לשאול שוב."
BAD_YEARS = "טווח השנים שנבחר אינו תקין: שנת הסיום מוקדמת משנת ההתחלה. לאיזו שנה הכוונה?"
PARTIAL_NOTE = "הזמן שהוקצב לשאלה הסתיים לפני שכל הצעדים הושלמו; מוצגת תוצאה חלקית."
FACT_SCOPE_NOTE = "הנתון מחושב רק מהמסמכים שבתחום שבהם נמצא ערך, ואינו מתאר את כלל המאגר."
TOOL_LABELS = {
    "search": "חיפוש בתוכן המסמכים המורשים", "compute_records": "חישוב SQL על רשומות מאומתות",
    "extract_and_compute": "חילוץ הנתון מהמסמכים עם ציטוט מדויק וחישוב בקוד", "compare": "השוואה בין מקורות",
    "locate": "איתור מסמכים", "explain_previous": "הסבר התשובה הקודמת", "show_sources": "הצגת מקורות",
}
CONTEXT_LABELS = {"attribute": "נתון", "metric": "מדד", "city": "עיר", "neighborhood": "שכונה", "years": "שנים",
                  "date_field": "שדה תאריך", "data_kind": "סוג הנתון", "property_type": "סוג נכס",
                  "area_type": "בסיס שטח", "vat_basis": "בסיס מע״מ"}
_FACT_OPS = {"weighted_mean": "mean", "none": "mean"}
_FACT_LABELS = dict(OPERATION_LABELS, count="מספר הערכים")


class TurnError(Exception):
    """A turn the client must change or retry (mapped to an HTTP status by the API)."""

    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code, self.detail = status_code, detail


@dataclass
class TurnRequest:
    question: str | None
    clarification: dict | None = None
    filters: dict | None = None
    remove: list[str] = field(default_factory=list)


@dataclass
class Loaded:
    """Inputs of a turn, read in one short transaction."""

    conversation_id: UUID
    state: ConversationState
    state_version: int
    gazetteer: Gazetteer
    attributes: list[dict]
    provider: LLMProvider | None  # the answering provider of the office's mode (cloud, demo mock or none)
    pstate: ProviderState
    conflict_pending: dict | None
    previous: dict | None  # the last answered question: text and turn plan (for chip edits)
    data_version: int
    settings_version: int

    @property
    def cloud_provider(self) -> LLMProvider | None:
        return self.provider if self.pstate.mode == Mode.CLOUD else None


@dataclass
class Interpreted:
    plan: TurnPlan | None
    mode: str  # rules | model | limited | button | edit
    question: str
    status: str | None = None  # provider status of the interpretation call
    limitation: str | None = None
    unknown_place: str | None = None
    free_text: bool = False
    answer: dict | None = None  # a direct answer that changes no state (re-asked conflict, invalid range)
    conflict: dict | None = None  # the filter conflict to keep open
    removed: list[str] = field(default_factory=list)  # removed chips outside the delta (attribute, metric)


@dataclass
class Part:
    answer: dict
    computed: list[ComputedValue] = field(default_factory=list)


@dataclass
class TurnResult:
    answer: dict
    state: ConversationState | None  # None: the state is unchanged
    plan_record: dict | None
    steps: list[dict]
    facts_versions: dict[str, int]
    intent: str | None
    route: str
    conditions: dict | None
    conflict_pending: dict | None = None
    cache_key: str | None = None
    cache_payload: dict | None = None


@dataclass
class _Run:
    ctx: TenantContext
    L: Loaded
    it: Interpreted
    plan: TurnPlan
    state: ConversationState
    question_id: UUID
    deadline: float
    attribute: AttributeDef | None = None
    steps: list[dict] = field(default_factory=list)
    statuses: list[str] = field(default_factory=list)
    facts_versions: dict[str, int] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)
    clarification: ProposedClarification | None = None
    cacheable: bool = True
    partial: bool = False  # the turn deadline cut a step short
    incomplete: bool = False  # versions in scope are not yet extracted (async jobs, cap)
    search_fallback: bool = False

    @property
    def question(self) -> str:
        return self.it.question

    def step(self, tool: str, args: dict, result: dict, summary: str, statuses: list[str] | None = None) -> None:
        self.steps.append({"tool": tool, "args": args, "result": result, "summary": summary,
                           "provider_statuses": statuses or []})

    def ask(self, key: str, question: str, options: list[dict]) -> None:
        self.clarification = ProposedClarification(key=key, question=question,
                                                   options=[ClarifyOption(**o) for o in options])


# --- loading ----------------------------------------------------------------------------------------------

def load(conn: Connection, conv) -> Loaded:
    """Everything the turn reads before any model call (inside the reservation transaction)."""
    try:
        state = ConversationState.model_validate(dict(conv.state or {}) | {"version": conv.state_version})
    except ValidationError:
        state = ConversationState(version=conv.state_version)
    provider, pstate = select_provider(conn)
    prev = conn.execute(text(
        "SELECT question_text, plan FROM questions WHERE conversation_id = :c AND status = 'done'"
        " AND answer_kind <> 'clarification' AND NOT coalesce((plan->>'meta')::boolean, false)"
        " AND plan ? 'turn_plan' ORDER BY created_at DESC LIMIT 1"), {"c": conv.id}).first()
    legacy = conv.pending_clarification or {}
    return Loaded(
        conversation_id=conv.id, state=state, state_version=conv.state_version, gazetteer=load_gazetteer(conn),
        attributes=list_attribute_handles(conn), provider=provider, pstate=pstate,
        conflict_pending=legacy if legacy.get("key") == FILTER_CONFLICT else None,
        previous={"question": prev.question_text, "turn_plan": prev.plan["turn_plan"]} if prev else None,
        data_version=current_data_version(conn),
        settings_version=conn.execute(text("SELECT settings_version FROM office_settings")).scalar_one(),
    )


def check_request(req: TurnRequest, L: Loaded) -> None:
    """Reject a request that cannot run, before the turn is reserved."""
    if any(key not in REMOVE_KEYS for key in req.remove):
        raise TurnError(422, "לא ניתן להסיר את התנאי המבוקש")
    if req.question:
        return
    if req.clarification:
        key = req.clarification.get("key")
        if key == FILTER_CONFLICT:
            if L.conflict_pending is None:
                raise TurnError(409, NO_PENDING)
        elif L.state.pending is None or L.state.pending.key != key:
            raise TurnError(409, NO_PENDING)
        return
    if not (_edits(req.filters) or req.remove) or L.previous is None:
        raise TurnError(422, NEED_QUESTION)


# --- interpretation ---------------------------------------------------------------------------------------

def _absolute(plan: TurnPlan, state: ConversationState) -> TurnPlan:
    """Resolve a relative year against the state the turn started from (KTD13 step 4)."""
    offset = plan.conditions.relative_year_offset
    if offset is None:
        return plan
    base = state.conditions
    if plan.turn_relation in ("answer_to_clarification", "change_clarification") and state.pending is not None:
        base = state.pending.conditions
    if base.year_from is None:
        return plan  # apply_turn asks which year
    delta = plan.conditions.model_copy(update={
        "relative_year_offset": None, "year_from": base.year_from + offset,
        "year_to": base.year_to + offset if base.year_to is not None else None})
    return plan.model_copy(update={"conditions": delta})


def _button(req: TurnRequest, L: Loaded) -> Interpreted:
    key, value = req.clarification.get("key"), req.clarification.get("value")
    if key == FILTER_CONFLICT:
        cp = L.conflict_pending
        if value not in {o["value"] for o in cp["options"]}:
            return Interpreted(None, "button", cp["question_text"], conflict=cp,
                               answer=clarification_answer(FILTER_CONFLICT, cp["question"], cp["options"]))
        name, chosen = value.split(":", 1)
        plan = TurnPlan.model_validate(cp["plan"])
        delta = plan.conditions.model_copy(update={name: int(chosen) if name.startswith("year") else chosen})
        return Interpreted(plan.model_copy(update={"conditions": delta}), "button", cp["question_text"])
    pending = L.state.pending
    question = pending.original_question or ""
    if value not in {o.value for o in pending.options}:  # re-ask in the same context
        return Interpreted(TurnPlan.build(task_type=pending.task_type or "compute",
                                          turn_relation="change_clarification"), "button", question)
    return Interpreted(answer_plan(pending, value), "button", question)


def _edit(L: Loaded) -> Interpreted:
    """Chip edits with no new question: re-run the last answered task with the edited conditions."""
    prev = TurnPlan.model_validate(L.previous["turn_plan"])
    task = prev.task_type if prev.task_type not in ("clarify", "abstain") else "answer"
    plan = TurnPlan.build(task_type=task, turn_relation="follow_up", metric=prev.metric, unit=prev.unit,
                          search_queries=prev.search_queries, steps=[s.model_dump() for s in prev.steps])
    return Interpreted(plan, "edit", L.previous["question"])


def _with_edits(it: Interpreted, req: TurnRequest) -> Interpreted:
    """Explicit condition edits join the plan's delta; one that contradicts what the question states asks."""
    if it.plan is None or not (_edits(req.filters) or req.remove):
        return it
    delta = it.plan.conditions.model_dump()
    for key, value in (req.filters or {}).items():
        if key not in EDIT_KEYS or value in (None, ""):  # anything else (an office id, say) is ignored
            continue
        if key in ("year_from", "year_to"):
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise TurnError(422, "ערך מסנן לא תקין") from None
        stated = delta.get(key)
        if stated is not None and stated != value:
            options = [{"value": f"{key}:{stated}", "label": f"לפי השאלה: {stated}"},
                       {"value": f"{key}:{value}", "label": f"לפי הסינון: {value}"}]
            question = CLARIFY_QUESTIONS[FILTER_CONFLICT]
            plan = it.plan.model_copy(update={"conditions": _delta(delta)})
            conflict = {"key": FILTER_CONFLICT, "question": question, "options": options,
                        "plan": plan.model_dump(mode="json"), "question_text": it.question}
            return Interpreted(None, it.mode, it.question, it.status, it.limitation, conflict=conflict,
                               answer=clarification_answer(FILTER_CONFLICT, question, options))
        delta[key] = value
        if key in ("year_from", "year_to"):
            delta["relative_year_offset"] = None
    for key in req.remove:
        clear = REMOVE_KEYS[key]
        if clear is None:
            continue
        delta["clear"] = list(dict.fromkeys([*delta["clear"], clear]))
        for k in (("year_from", "year_to", "relative_year_offset") if clear == "years" else (clear,)):
            delta[k] = None
    if delta["year_from"] is not None and delta["year_to"] is not None and delta["year_to"] < delta["year_from"]:
        return Interpreted(None, it.mode, it.question, it.status,
                           answer=clarification_answer("referent", BAD_YEARS, []))
    return Interpreted(it.plan.model_copy(update={"conditions": _delta(delta)}), it.mode, it.question,
                       it.status, it.limitation, it.unknown_place, it.free_text,
                       removed=[k for k in req.remove if REMOVE_KEYS[k] is None])


def _edits(filters: dict | None) -> dict:
    return {k: v for k, v in (filters or {}).items() if k in EDIT_KEYS and v not in (None, "")}


def _delta(delta: dict) -> ConditionDelta:
    try:
        return ConditionDelta.model_validate(delta)
    except ValidationError:
        raise TurnError(422, "ערך מסנן לא תקין") from None


def interpret_turn(ctx: TenantContext, req: TurnRequest, L: Loaded) -> Interpreted:
    """The validated plan of this turn (no transaction is open while the model interprets)."""
    question = (req.question or "").strip()
    if req.clarification and not question:
        it = _button(req, L)
    elif not question:
        it = _edit(L)
    else:
        result = interpret(question, L.state, L.gazetteer, L.attributes, L.cloud_provider)
        if result.status is not None:
            with tenant_tx(ctx) as conn:
                log_usage(conn, L.cloud_provider, Purpose.INTERPRET.value, result, result.ok, result.status)
        limitation = result.limitation
        if result.mode == "limited" and L.pstate.mode == Mode.ERROR:
            limitation = L.pstate.limitation()
        plan = result.plan
        if (plan is not None and result.mode == "model" and plan.clarification is not None and plan.steps
                and plan.clarification.key in RECORD_CLARIFY_KEYS):
            # Record clarifications are asked by the server only when they change the result (R20).
            plan = plan.model_copy(update={"clarification": None, "task_type": plan.task_type
                                           if plan.task_type != "clarify" else "compute"})
        it = Interpreted(plan, result.mode, question, str(result.status) if result.status else None, limitation,
                         result.unknown_place, free_text=True)
    it = _with_edits(it, req)
    if it.plan is not None:
        it.plan = _absolute(it.plan, L.state)
    return it


# --- execution --------------------------------------------------------------------------------------------

def _expand_versions(ctx: TenantContext, plan: TurnPlan,
                     state: ConversationState) -> tuple[TurnPlan, ConversationState]:
    """A comparison that names one source of a document with several versions compares its two latest
    visible versions (AE6); the older one gets its own handle."""
    steps = list(plan.steps)
    for i, step in enumerate(steps):
        handles = list(dict.fromkeys(step.source_handles))
        if step.tool != "compare" or len(handles) != 1 or handles[0] not in state.sources:
            continue
        ref = state.sources[handles[0]]
        with tenant_tx(ctx) as conn:
            versions = conn.execute(text(
                "SELECT v.id FROM document_versions v JOIN documents d ON d.id = v.document_id"
                " AND d.deleted_at IS NULL WHERE v.document_id = :d ORDER BY v.version_no DESC LIMIT 2"),
                {"d": ref.document_id}).scalars().all()
        if len(versions) < 2:
            continue
        refs = [SourceRef(document_id=ref.document_id, version_id=str(v)) for v in reversed(versions)]
        state, new = remember_sources(state, refs)
        steps[i] = step.model_copy(update={"source_handles": new})
    return plan.model_copy(update={"steps": steps}), state


def _plan_attribute(run: _Run) -> AttributeDef | None:
    """The attribute of the plan's computation: the model's handle or description, else the state's, else
    the price per m² of the rules path."""
    step = next((s for s in run.plan.steps if s.tool in COMPUTE_TOOLS), None)
    if step is None:
        return None
    ref = run.plan.attribute
    handle = step.attribute_handle or (ref.handle if ref else None)
    description = ref.description if ref else None
    dimension = ref.unit_dimension if ref else None
    if not handle and not description and run.state.attribute is not None:
        st = run.state.attribute
        handle, description, dimension = st.handle, st.description, st.unit_dimension
    with tenant_tx(run.ctx) as conn:
        try:
            attr = resolve_attribute(conn, handle=handle, description=description or DEFAULT_ATTRIBUTE_LABEL,
                                     unit_dimension=dimension, handles=handle_map(run.L.attributes))
        except ValueError:
            return None
    shown = next((a["handle"] for a in run.L.attributes if a["id"] == attr.id), None)
    run.state = run.state.model_copy(update={"attribute": AttributeRef(
        handle=shown, description=attr.label, unit_dimension=attr.unit_dimension)})
    return attr


def _filters(run: _Run) -> MetadataFilters | None:
    s = run.state.conditions
    f = MetadataFilters(city=s.city, neighborhood=s.neighborhood, year_from=s.year_from, year_to=s.year_to,
                        date_field=s.date_field or "valuation_date")
    return f if f.active else None


def _records(run: _Run, attr: AttributeDef) -> Part | None:
    """A structured attribute over unique verified records (parametric SQL, no model call)."""
    c = run.state.query_conditions("calculation")
    monetary = attr.unit_dimension in MONETARY_DIMENSIONS
    metric = run.state.metric or "mean"
    op = metric if metric != "none" else "mean"
    if op == "weighted_mean" and attr.structured_column != "transactions.price_per_sqm":
        op = "mean"
    with tenant_tx(run.ctx) as conn:
        if monetary:
            missing = missing_conditions(c)
            key = missing[0] if missing else mixed_basis(conn, c)
            if key:
                run.ask(key, CLARIFY_QUESTIONS[key], clarification_options(conn, key, c))
                return None
        else:
            c, key = _record_gap(conn, c, attr, op)
            if key:
                run.ask(key, CLARIFY_QUESTIONS[key], clarification_options(conn, key, c, monetary=False))
                return None
        args = {"attribute": attr.key, "operation": op, "conditions": c.normalized_key()}
        if attr.structured_column == "transactions.price_per_sqm" and op in ("mean", "weighted_mean", "median"):
            answer, _rows = numeric_answer(conn, c)
            n = (answer.get("numeric") or {}).get("record_count", 0)
            computed = computed_from_numeric(answer["numeric"], [s["evidence_id"] for s in answer["sources"]]) \
                if answer["numeric"] else []
            run.step("compute_records", args, {"count": n}, f"{n} רשומות מאומתות ייחודיות")
            return Part(answer, computed)
        r = compute_records(conn, c, attr.structured_column, op)
        cov = coverage(conn, c)
        if r.count == 0:
            run.step("compute_records", args, {"count": 0}, "לא נמצאו רשומות מאומתות תואמות")
            return Part({"kind": "abstain", "text": abstain_text(c, cov["records_awaiting_verification"]),
                         "provider": "template", "demo": False, "numeric": None, "sources": [], "coverage": cov,
                         "limitations": ["לא קיים בסיס מספיק במאגר המשרד; לא הוצג מספר."]})
        rows = sources_for(conn, r.transaction_ids)
        body, limitations = structured_text(c, attr.label, op, r, attr.canonical_unit,
                                            uncertain_duplicates(conn, r.transaction_ids))
    sources = source_json_rows(rows)
    ids = [s["evidence_id"] for s in sources]
    shown = r.values if (r.count <= 5 or op == "values") else None
    numeric = {"conditions": c.describe(), "attribute": attr.label, "operation": op, "value": _str(r.value),
               "unit": attr.canonical_unit, "record_count": r.count, "minimum": _str(r.minimum),
               "maximum": _str(r.maximum), "values": [str(v) for v in shown] if shown is not None else None}
    computed = [ComputedValue("C1", "מספר הרשומות בחישוב", number(r.count), ids)]
    if r.value is not None and op not in ("count", "values"):
        computed.append(ComputedValue("C2", f"{OPERATION_LABELS[op]} {attr.label}",
                                      _with_unit(r.value, attr.canonical_unit), ids))
    run.step("compute_records", args, {"count": r.count, "value": _str(r.value)},
             f"{OPERATION_LABELS[op]} {attr.label} על {r.count} רשומות מאומתות ייחודיות")
    return Part({"kind": "numeric", "text": body, "provider": "template", "demo": False, "numeric": numeric,
                 "sources": sources, "coverage": cov, "limitations": limitations}, computed)


def _record_gap(conn: Connection, c: QueryConditions, attr: AttributeDef,
                op: str) -> tuple[QueryConditions, str | None]:
    """For a record attribute that is not money: ask which records or which date only when the choice
    changes the result; otherwise take the only choice that has records (R20)."""
    col = attr.structured_column
    if c.data_kind is None:
        kinds = [k for k in ("transaction_price", "appraised_value")
                 if compute_records(conn, c.model_copy(update={"data_kind": k}), col, "count").count]
        if len(kinds) > 1:
            return c, "data_kind"
        c = c.model_copy(update={"data_kind": kinds[0] if kinds else "transaction_price"})
    if c.year_from is not None and c.date_field is None:
        results = set()
        for f in ("transaction_date", "valuation_date", "report_date"):
            r = compute_records(conn, c.model_copy(update={"date_field": f}), col, op)
            results.add((r.count, r.value))
        if len(results) > 1:
            return c, "date_field"
        c = c.model_copy(update={"date_field": "transaction_date" if c.data_kind == "transaction_price"
                                 else "valuation_date"})
    return c, None


def _facts(run: _Run, attr: AttributeDef) -> Part | None:
    """An attribute nobody anticipated: extracted with verbatim provenance, computed in code (KTD8, KTD9)."""
    op = _FACT_OPS.get(run.state.metric or "none", run.state.metric or "mean")
    if op not in facts.OPERATIONS:
        op = "mean"
    filters = _filters(run)
    comp = facts.extract_and_compute(None, run.ctx, attr, filters, op, provider=run.L.cloud_provider,
                                     deadline=run.deadline)
    if comp.facts_version is not None:
        run.facts_versions[str(attr.id)] = comp.facts_version
    run.statuses += comp.provider_statuses
    run.cacheable = run.cacheable and comp.cacheable
    run.partial = run.partial or comp.deadline_reached
    run.incomplete = run.incomplete or comp.partial
    cov = comp.coverage
    run.step("extract_and_compute", {"attribute": attr.key, "operation": op,
                                     "filters": filters.__dict__ if filters else None},
             {"coverage": cov, "main_n": comp.main.n, "preliminary_n": comp.preliminary.n if comp.preliminary else 0,
              "extraction_unavailable": comp.extraction_unavailable},
             f"{cov['in_scope']} מסמכים בתחום, ערך נמצא ב-{cov['found']}", comp.provider_statuses)
    if comp.extraction_unavailable and comp.main.n == 0:
        # computing a new attribute needs the cloud model or reviewed data: show relevant passages (AE2)
        run.search_fallback = True
        run.limitations.append(LIMITED_MODE_NOTE if run.L.pstate.mode != Mode.ERROR else run.L.pstate.limitation())
        return None
    with tenant_tx(run.ctx) as conn:
        general = coverage(conn, None)
    return _fact_part(comp, attr, op, run.state.query_conditions(), general)


def _fact_part(comp, attr: AttributeDef, op: str, c: QueryConditions, general: dict) -> Part:
    sources = []
    for i, s in enumerate(comp.sources, start=1):
        page = s.get("page")
        sources.append({"evidence_id": f"E{i}", "document_id": str(s["document_id"]),
                        "version_id": str(s["version_id"]), "title": s["title"], "page_list": [page] if page else [],
                        "section": None, "row": None, "snippet": s["quote"], "value": _str(s["value"]),
                        "tier": s["tier"], "url": source_file_url(s["document_id"], s["version_id"], page)})
    by_tier = {t: [x["evidence_id"] for x in sources if x["tier"] == t] for t in ("verified", "preliminary")}
    unit = comp.unit
    label = _FACT_LABELS[op]
    where = ", ".join(x["value"] for x in c.describe() if x["label"] in ("עיר", "שכונה"))
    lines, computed = [], []
    if comp.main.n:
        lines.append(f"{label} {comp.attribute_label} לפי ערכים שאומתו: {_figure(comp.main, op, unit)}"
                     f" (מבוסס על {comp.main.n} תצפיות{', ' + where if where else ''}).")
        computed.append(ComputedValue("C1", f"{label} {comp.attribute_label} (ערכים שאומתו)",
                                      _figure(comp.main, op, unit), by_tier["verified"]))
    if comp.preliminary is not None:
        lines.append(f"נתון ראשוני, כולל ערכים שחולצו אוטומטית וטרם נבדקו בידי אדם: "
                     f"{label} {comp.attribute_label} {_figure(comp.preliminary, op, unit)}"
                     f" (מבוסס על {comp.preliminary.n} תצפיות{', ' + where if where else ''}).")
        computed.append(ComputedValue(f"C{len(computed) + 1}", f"{label} {comp.attribute_label} (נתון ראשוני)",
                                      _figure(comp.preliminary, op, unit),
                                      by_tier["verified"] + by_tier["preliminary"]))
    cov = comp.coverage
    found_any = bool(comp.main.n or comp.preliminary is not None)
    kind = None
    if not found_any:
        kind = ("not_extracted_or_verified" if cov["not_yet_extracted"] or cov["awaiting_review"]
                else "not_found" if cov["in_scope"] == 0 else "not_stated")
        lines.append(ABSTENTION_TEXT[kind])
    lines.append(facts.coverage_text(cov))
    limitations = [FACT_SCOPE_NOTE]
    if comp.partial:
        limitations.append("חלק מהמסמכים שבתחום טרם חולצו, ולכן התוצאה חלקית.")
    if comp.pending_jobs:
        limitations.append(f"{comp.pending_jobs} מסמכים ממתינים לחילוץ ברקע; שאלו שוב מאוחר יותר.")
    if comp.conflicts:
        limitations.append("נמצאו ערכים סותרים לאותו נכס במסמכים שונים; הם הועברו לבדיקה ולא נכללו.")
    figure = comp.main if comp.main.n else None
    numeric = {"conditions": c.describe(), "attribute": comp.attribute_label, "operation": op, "unit": unit,
               "value": _str(figure.value) if figure else None, "record_count": comp.main.n,
               "values": [str(v) for v in figure.values] if figure and figure.values is not None else None}
    pre = comp.preliminary
    answer = {
        "kind": "numeric" if found_any else "abstain", "text": "\n".join(lines), "provider": "template",
        "demo": False, "numeric": numeric if found_any else None, "sources": sources,
        "coverage": general | {"facts": cov}, "limitations": limitations, "abstention_kind": kind,
        "preliminary": {"value": _str(pre.value), "record_count": pre.n,
                        "values": [str(v) for v in pre.values] if pre.values is not None else None} if pre else None,
        "pending_extraction": cov["not_yet_extracted"],
    }
    return Part(answer, computed)


def _search_queries(run: _Run, combined: bool) -> tuple[list[str], list[str], MetadataFilters | None]:
    s = run.state.conditions
    places = [p for p in (s.city, s.neighborhood) if p]
    queries = [q for q in run.plan.search_queries if q and q.strip()] or [run.question]
    if run.search_fallback and run.attribute is not None:
        queries = [run.question, run.attribute.label]
    # The model's places and years scope a content search; the rules' places only rank (as before).
    scoped = run.it.mode == "model" and not combined
    if combined and places:
        queries = [f"{q} {' '.join(places)}" for q in queries]
    return queries, places, _filters(run) if scoped else None


def _search(run: _Run, base: Part | None) -> dict:
    """Search authorized content and compose an answer from verified claims; with ``base`` (a computation)
    the result is the combined answer and the model sees the computed values by handle only."""
    queries, places, filters = _search_queries(run, base is not None)
    base_sources = base.answer["sources"] if base else []
    with tenant_tx(run.ctx) as conn:
        out = search_evidence(conn, queries, EVIDENCE_LIMIT * 2, filters=filters, place_terms=places)
        hits = [h for h in out.hits if h["lexical_support"]][:EVIDENCE_LIMIT]
        evidence = evidence_from_hits(hits, len(base_sources) + 1)
        cov = base.answer["coverage"] if base else coverage(conn, None)
    unknown = out.filter_report.unknown_count if out.filter_report else 0
    args = {"queries": queries, "filters": filters.__dict__ if filters else None}
    limitations = list(base.answer["limitations"]) if base else []
    if unknown:
        limitations.append(f"{unknown} מסמכים ללא נתוני מקום או תאריך לא נכללו בחיפוש.")
    if not evidence:
        run.step("search", args, {"evidence": 0}, "לא נמצאו קטעים רלוונטיים")
        if base:
            answer = dict(base.answer)
            answer["kind"] = "combined"
            answer["limitations"] = limitations + ["לא נמצאו קטעי הסבר רלוונטיים במסמכים המורשים."]
            return answer
        kind = no_evidence_kind(run.ctx)
        return {"kind": "abstain", "provider": "template", "demo": False, "sources": [], "coverage": cov,
                "text": ABSTENTION_TEXT[kind], "numeric": None,
                "limitations": limitations + ["החיפוש בוצע רק במסמכי המשרד שעובדו ושאתם מורשים לראות."],
                "claims": [], "abstention_kind": kind, "dropped_claims": 0}
    provider = run.L.provider
    if provider is not None and time.monotonic() >= run.deadline:
        provider, run.partial = None, True
    comp = compose_answer(provider, run.question, evidence, computed=base.computed if base else [],
                          cited_extra={s["evidence_id"]: s.get("snippet") or "" for s in base_sources})
    statuses = [str(u.status) for u in comp.usage]
    if comp.usage:
        with tenant_tx(run.ctx) as conn:
            log_usages(conn, provider, comp.usage)
    run.statuses += statuses
    run.cacheable = run.cacheable and comp.cacheable
    run.step("search", args, {"evidence": len(evidence), "claims": len(comp.claims), "provider": comp.provider},
             f"{len(evidence)} קטעי ראיה מתוך המסמכים המורשים", statuses)
    limitations += comp.limitations
    sources = base_sources + source_json(evidence)
    fields = answer_fields(comp, run.L.pstate.mode.value)
    if base:
        answer = dict(base.answer)
        answer.update({"kind": "combined", "text": base.answer["text"] + "\n\n" + comp.text,
                       "provider": comp.provider if comp.provider != "extractive" else "template",
                       "demo": comp.demo, "sources": sources, "limitations": limitations, **fields})
        return answer
    return {"kind": "content", "text": comp.text, "provider": comp.provider, "demo": comp.demo, "sources": sources,
            "coverage": cov, "limitations": limitations, "numeric": None, **fields}


def _locate(run: _Run) -> dict:
    """The documents (and pages) where the subject appears; no model call."""
    queries, places, filters = _search_queries(run, False)
    with tenant_tx(run.ctx) as conn:
        out = search_evidence(conn, queries, EVIDENCE_LIMIT * 2, filters=filters, place_terms=places)
        cov = coverage(conn, None)
    evidence = evidence_from_hits([h for h in out.hits if h["lexical_support"]][:EVIDENCE_LIMIT * 2], 1)
    run.step("locate", {"queries": queries, "filters": filters.__dict__ if filters else None},
             {"evidence": len(evidence)}, f"{len({e['document_id'] for e in evidence})} מסמכים")
    if not evidence:
        kind = no_evidence_kind(run.ctx)
        return {"kind": "abstain", "provider": "template", "demo": False, "sources": [], "coverage": cov,
                "text": ABSTENTION_TEXT[kind], "numeric": None, "claims": [], "abstention_kind": kind,
                "limitations": ["החיפוש בוצע רק במסמכי המשרד שעובדו ושאתם מורשים לראות."]}
    docs: dict[str, dict] = {}
    for e in evidence:
        d = docs.setdefault(e["document_id"], {"title": e["title"], "pages": [], "ids": []})
        d["pages"] += [p for p in e["page_list"] if p not in d["pages"]]
        d["ids"].append(e["evidence_id"])
    lines = ["המסמכים שבהם נמצאו קטעים רלוונטיים:"]
    for d in docs.values():
        pages = f" — עמ׳ {', '.join(map(str, sorted(d['pages'])))}" if d["pages"] else ""
        lines.append(f"• {d['title']}{pages} {' '.join(f'[{i}]' for i in d['ids'])}")
    return {"kind": "content", "text": "\n".join(lines), "provider": "template", "demo": False,
            "sources": source_json(evidence), "coverage": cov, "numeric": None, "claims": [],
            "abstention_kind": None, "limitations": ["רשימת המסמכים מבוססת על חיפוש בתוכן המסמכים המורשים."]}


def _compare(run: _Run, step: Step) -> dict | None:
    """Two sides from the conversation's source handles (R10, R11)."""
    refs = [run.state.sources[h] for h in dict.fromkeys(step.source_handles) if h in run.state.sources]
    same_doc = len({r.document_id for r in refs}) == 1
    if same_doc:
        sides = [CompareSide(version_id=UUID(r.version_id)) for r in {r.version_id: r for r in refs}.values()]
    else:
        sides = [CompareSide(document_id=UUID(d)) for d in dict.fromkeys(r.document_id for r in refs)]
    if len(sides) < 2:
        others = [h for h in run.state.sources if h not in step.source_handles]
        run.ask("referent", REFERENT_COMPARE_SECOND, [{"value": h, "label": f"מקור {h}"} for h in others])
        return None
    # Gather per-side evidence in a short transaction, compose (answer and judge calls) with no connection,
    # then log the calls in a final short transaction (R26, KTD13).
    with tenant_tx(run.ctx) as conn:
        gathered = gather_sides(conn, run.question, sides, queries=run.plan.search_queries)
    provider = run.L.provider
    if provider is not None and not gathered.missing and time.monotonic() >= run.deadline:
        provider, run.partial = None, True
    out = compose_comparison(gathered, provider, run.L.pstate)
    if out.usage:
        with tenant_tx(run.ctx) as conn:
            log_usages(conn, provider, out.usage)
    statuses = [str(u.status) for u in out.usage]
    run.statuses += statuses
    run.cacheable = run.cacheable and out.cacheable
    info = out.answer.get("compare", {})
    run.step("compare", {"sides": [str(s.version_id or s.document_id) for s in sides]},
             {"incomplete": info.get("incomplete"), "sides": [s["evidence_count"] for s in info.get("sides", [])]},
             "השוואה בין " + " ו-".join(s["label"] for s in info.get("sides", [])), statuses)
    return out.answer


def _previous_answer(run: _Run):
    with tenant_tx(run.ctx) as conn:
        return conn.execute(text(
            "SELECT question_text, plan, steps, answer, conditions FROM questions WHERE conversation_id = :c"
            " AND status = 'done' AND id <> :q AND answer_kind <> 'clarification'"
            " AND NOT coalesce((plan->>'meta')::boolean, false) ORDER BY created_at DESC LIMIT 1"),
            {"c": run.L.conversation_id, "q": run.question_id}).first()


def _reauthorized(run: _Run, sources: list[dict]) -> tuple[list[dict], int]:
    with tenant_tx(run.ctx) as conn:
        ok = [s for s in sources if sources_still_authorized(conn, [s])]
    return ok, len(sources) - len(ok)


def _meta(run: _Run, why: bool) -> dict:
    """"למה?" and "תראה לי את המקור": the previous answer's method and sources, re-authorized now; no
    model call and never a previous answer as a factual source (R19)."""
    prev = _previous_answer(run)
    tool = "explain_previous" if why else "show_sources"
    base = {"provider": "template", "demo": False, "coverage": None, "numeric": None, "claims": [],
            "abstention_kind": None, "meta": tool}
    if prev is None:
        run.step(tool, {}, {"previous": False}, "אין תשובה קודמת")
        return base | {"kind": "abstain", "text": "אין בשיחה זו תשובה קודמת.", "sources": [], "limitations": []}
    sources, gone = _reauthorized(run, (prev.answer or {}).get("sources", []))
    run.step(tool, {}, {"sources": len(sources), "unavailable": gone}, f"{len(sources)} מקורות זמינים")
    gone_note = (f"{gone} מקורות של התשובה הקודמת כבר אינם זמינים: המסמך נמחק, הוחלף בגרסה חדשה או שאין לכם"
                 " עוד הרשאה לראותו.") if gone else None
    if why:
        lines = [f"כך התקבלה התשובה לשאלה \"{prev.question_text}\":"]
        for st in prev.steps or []:
            lines.append(f"• {TOOL_LABELS.get(st.get('tool'), st.get('tool'))}: {st.get('summary') or ''}".rstrip(": "))
        conds = QueryConditions.model_validate(prev.conditions).describe() if prev.conditions else []
        if conds:
            lines.append("תנאים: " + "; ".join(f"{x['label']}: {x['value']}" for x in conds) + ".")
        lines.append(f"מקורות שנבדקו מחדש וזמינים: {len(sources)}." + (f" {gone_note}" if gone_note else ""))
        limitations = ["ההסבר מתאר את השיטה ואת המקורות של התשובה הקודמת; הוא אינו מקור עובדתי בפני עצמו."]
        return base | {"kind": "content", "text": "\n".join(lines), "sources": sources, "limitations": limitations}
    if not sources:
        text_ = "המקור של התשובה הקודמת כבר אינו זמין." + (f" {gone_note}" if gone_note else "")
        return base | {"kind": "abstain", "text": text_, "sources": [], "limitations": []}
    lines = ["המקורות של התשובה הקודמת:"]
    for s in sources:
        pages = f", עמ׳ {', '.join(map(str, s['page_list']))}" if s.get("page_list") else ""
        lines.append(f"• {s['title']}{pages} [{s['evidence_id']}]")
    if gone_note:
        lines.append(gone_note)
    return base | {"kind": "content", "text": "\n".join(lines), "sources": sources, "limitations": []}


def _unknown_place(run: _Run) -> dict:
    with tenant_tx(run.ctx) as conn:
        cov = coverage(conn, None)
    name = run.it.unknown_place
    body = (f"אין במאגר המשרד רשומות עבור \"{name}\". לא חושב מספר." if name
            else "לא ניתן לענות על השאלה על סמך מסמכי המשרד.")
    run.step("abstain", {}, {"unknown_place": name}, "מקום שאינו במאגר המשרד" if name else "אין דרך לענות")
    return {"kind": "abstain", "provider": "template", "demo": False, "sources": [], "coverage": cov, "text": body,
            "numeric": None, "abstention_kind": "not_found",
            "limitations": ["המערכת עונה רק על סמך מסמכי המשרד ואינה משלימה מידע ממקורות אחרים."]}


FOLLOW_UP_WITHOUT_CONTEXT = "לאיזה נושא מתייחסת שאלת ההמשך? בשיחה זו אין עדיין שאלה קודמת; כתבו את השאלה המלאה."


def _has_context(state) -> bool:
    """A follow-up needs something to follow: an earlier task, topic, attribute or stated condition."""
    c = state.conditions
    return bool(state.task_type or state.topic or state.attribute or state.recent_questions
                or any(getattr(c, k, None) is not None for k in type(c).model_fields))


def _default_steps(plan: TurnPlan) -> list[Step]:
    if plan.steps:
        return list(plan.steps)
    tool = {"compute": "compute_records", "compute_explain": "compute_records", "locate": "locate",
            "compare": "compare"}.get(plan.task_type, "search")
    steps = [Step(tool=tool, attribute_handle=None, source_handles=[])]
    if plan.task_type == "compute_explain":
        steps.append(Step(tool="search", attribute_handle=None, source_handles=[]))
    return steps


def _execute_steps(run: _Run, steps: list[Step]) -> dict | None:
    """At most four steps; the first computation runs first so a search can explain it (combined)."""
    steps = sorted(steps[:MAX_STEPS], key=lambda s: s.tool not in COMPUTE_TOOLS)
    tools = {s.tool for s in steps}
    if {"search", "locate"} <= tools:  # one presentation: a document list only when documents were asked for
        drop = "search" if run.plan.task_type == "locate" else "locate"
        steps = [s for s in steps if s.tool != drop]
    base: Part | None = None
    answer: dict | None = None
    computed = searched = False
    for step in steps:
        if step.tool in COMPUTE_TOOLS:
            if computed:
                continue
            computed = True
            attr = run.attribute
            if attr is None:
                continue
            base = _records(run, attr) if attr.source == "structured" else _facts(run, attr)
            if run.clarification is not None:
                return None
            if base is not None:
                answer = base.answer
        elif step.tool == "search":
            if not searched:
                searched = True
                answer = _search(run, base)
        elif step.tool == "locate":
            answer = _locate(run)
        elif step.tool == "compare":
            answer = _compare(run, step)
            if run.clarification is not None:
                return None
    if answer is None and (run.search_fallback or not searched):
        answer = _search(run, None)
    return answer


def _empty(answer: dict | None) -> bool:
    return answer is None or (answer["kind"] == "abstain" and not answer.get("sources") and not answer.get("numeric"))


def _replan(run: _Run) -> TurnPlan | None:
    """One replan in cloud mode when the steps returned nothing; the model sees step summaries only."""
    provider = run.L.cloud_provider
    if provider is None or time.monotonic() >= run.deadline:
        return None
    summaries = [{"tool": s["tool"], "summary": s["summary"]} for s in run.steps]
    payload = json.loads(build_interpret_input(run.question, run.state, run.L.gazetteer, run.L.attributes))
    payload["previous_steps"] = summaries
    payload["replan_note"] = "הצעדים הקודמים לא החזירו תוצאה. הצע ניסוח חיפוש או צעדים אחרים."
    result = provider.structured(Purpose.INTERPRET, INTERPRET_INSTRUCTIONS, json.dumps(payload, ensure_ascii=False),
                                 TurnPlan)
    with tenant_tx(run.ctx) as conn:
        log_usage(conn, provider, Purpose.INTERPRET.value, result, result.ok)
    run.statuses.append(str(result.status))
    if not result.ok:
        return None
    check = _validate_plan(result.parsed, gazetteer=run.L.gazetteer, attributes=run.L.attributes,
                           source_handles=run.state.sources)
    return check.plan if check.ok and check.plan.steps else None


# --- cache ------------------------------------------------------------------------------------------------

def scope_hash(ctx: TenantContext) -> str:
    scope = "admin" if ctx.is_admin else "employee:" + ",".join(sorted(str(g) for g in ctx.group_ids))
    return hashlib.sha256(scope.encode()).hexdigest()[:32]


def cache_key(run: _Run, facts_version: int | None) -> str:
    """The canonical validated task plus every version it depends on (KTD14)."""
    plan, s, attr, L = run.plan, run.state, run.attribute, run.L
    tools = sorted({st.tool for st in _default_steps(plan)})
    content = any(t in CONTENT_TOOLS for t in tools)
    extracted = attr is not None and attr.source != "structured"
    payload = {
        "office": str(run.ctx.office_id), "scope": scope_hash(run.ctx), "task": plan.task_type, "tools": tools,
        "attribute": [attr.key, facts.extraction_version(attr) if extracted else None] if attr else None,
        "facts_version": facts_version if extracted else None,
        "metric": s.metric, "unit": s.unit, "conditions": s.conditions.model_dump(),
        "query": [" ".join(q.split()) for q in (plan.search_queries or [run.question])] if content else None,
        "sources": sorted(s.sources[h].version_id for st in plan.steps for h in st.source_handles
                          if h in s.sources),
        "data_version": L.data_version, "settings_version": L.settings_version,
        "provider": [L.pstate.mode.value, L.provider.name, L.provider.model] if L.provider else [L.pstate.mode.value],
        "prompt": ANSWER_PROMPT_VERSION, "embedding": get_embedding_provider().model_id, "template": TEMPLATE_VERSION,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _cached(run: _Run) -> dict | None:
    """A cached answer whose sources are all still authorized, with coverage recomputed (R21)."""
    attr = run.attribute
    with tenant_tx(run.ctx) as conn:
        fv = conn.execute(text("SELECT facts_version FROM attribute_definitions WHERE id = :a"),
                          {"a": attr.id}).scalar() if attr is not None else None
        payload = conn.execute(text("SELECT payload FROM answer_cache WHERE cache_key = :k"),
                               {"k": cache_key(run, fv)}).scalar()
        if not payload or "answer" not in payload:
            return None
        answer = payload["answer"]
        if answer.get("kind") == "clarification" or not sources_still_authorized(conn, answer.get("sources", [])):
            return None
        c = run.state.query_conditions()
        cov = coverage(conn, c if c.data_kind else None)
        if attr is not None and attr.source != "structured" and (answer.get("coverage") or {}).get("facts"):
            op = (answer.get("numeric") or {}).get("operation") or "mean"
            cov["facts"] = facts.compute_facts(conn, attr, _filters(run), op if op in facts.OPERATIONS
                                               else "mean").coverage
    run.steps = list(payload.get("steps") or [])
    run.facts_versions = dict(payload.get("facts_versions") or {})
    return answer | {"coverage": cov, "cached": True}


# --- the turn ---------------------------------------------------------------------------------------------

def _intent(task: str | None) -> str | None:
    return {"compute": "calculation", "compute_explain": "combined", "answer": "explanation", "compare": "explanation",
            "locate": "document_lookup"}.get(task or "")


def _str(value) -> str | None:
    return None if value is None else str(value)


def _with_unit(value, unit: str | None) -> str:
    label = UNIT_LABELS.get(unit or "", "")
    shown = number(Decimal(str(value)) if not isinstance(value, int) else value)
    return f"{shown} {label}" if label else shown


def _figure(fig, op: str, unit: str | None) -> str:
    if op == "values":
        return ", ".join(_with_unit(v, unit) for v in fig.values or []) or "—"
    if op == "count":
        return number(fig.value)
    if op == "range" and fig.minimum is not None:
        return f"{_with_unit(fig.minimum, unit)}–{_with_unit(fig.maximum, unit)}"
    return "—" if fig.value is None else _with_unit(fig.value, unit)


def context_items(state: ConversationState) -> list[dict]:
    """The state's context as chips: key (what ``remove`` takes), Hebrew label and display value."""
    out = []
    if state.attribute is not None and (state.attribute.description or state.attribute.handle):
        out.append({"key": "attribute", "label": CONTEXT_LABELS["attribute"],
                    "value": state.attribute.description or state.attribute.handle})
    if state.metric and state.metric != "none":
        out.append({"key": "metric", "label": CONTEXT_LABELS["metric"],
                    "value": OPERATION_LABELS.get(state.metric, state.metric)})
    described = {d["label"]: d["value"] for d in state.query_conditions().describe()}
    c = state.conditions
    for key, value in (("data_kind", c.data_kind), ("city", c.city), ("neighborhood", c.neighborhood)):
        if value:
            out.append({"key": key, "label": CONTEXT_LABELS[key],
                        "value": described.get(CONTEXT_LABELS[key], value) if key != "data_kind"
                        else described.get("סוג הנתון", value)})
    if c.year_from is not None:
        years = str(c.year_from) if c.year_to in (None, c.year_from) else f"{c.year_from}–{c.year_to}"
        out.append({"key": "years", "label": CONTEXT_LABELS["years"], "value": years})
    if c.date_field:
        out.append({"key": "date_field", "label": CONTEXT_LABELS["date_field"], "value": DATE_FIELD_LABELS[c.date_field]})
    for key in ("property_type", "area_type", "vat_basis"):
        if getattr(c, key):
            label = CONTEXT_LABELS[key]
            out.append({"key": key, "label": label, "value": described.get(label, getattr(c, key))})
    return out


def _cleared_items(before: ConversationState, keys: list[str]) -> list[dict]:
    items = {i["key"]: i for i in context_items(before)}
    out = []
    for key in keys:
        k = "years" if key in ("year_from", "year_to") else key
        if k in items and items[k] not in out:
            out.append(items[k])
    return out


def _method(steps: list[dict]) -> str | None:
    lines = [f"{TOOL_LABELS.get(s['tool'], s['tool'])}: {s.get('summary') or ''}".rstrip(": ") for s in steps
             if s.get("tool") in TOOL_LABELS]
    return "; ".join(lines) or None


def _note(L: Loaded, it: Interpreted, state: ConversationState, resolved) -> str | None:
    """A one-line interpretation note for the UI: how a typed reply was read, or that a clarification is
    still open."""
    before = L.state.pending
    if before is None or not it.free_text:
        return None
    if resolved is not None:
        label = next((o.label for o in before.options if o.value == resolved[1]), resolved[1])
        return f"התשובה הובנה כמענה לשאלת ההבהרה: {label}."
    if state.pending is not None and state.pending.key == before.key and state.pending.question == before.question:
        return f"שאלת ההבהרה הקודמת עדיין פתוחה: {before.question}"
    return None


def execute_turn(ctx: TenantContext, it: Interpreted, L: Loaded, question_id: UUID, started: float) -> TurnResult:
    """Apply the interpreted plan to ``L.state`` and execute it (each tool in its own short transaction)."""
    route = it.mode
    plan_record = {"mode": it.mode, "status": it.status, "meta": False,
                   "turn_plan": it.plan.model_dump(mode="json") if it.plan else None}
    if it.answer is not None:  # a direct clarification that changes no state
        answer = _finish(it.answer, L, it, [], None, [], False)
        return TurnResult(answer, None, None, [], {}, None, route, None, conflict_pending=it.conflict)

    if it.mode == "model" and it.plan is not None:
        it.plan = normalize_model_plan(it.plan)
        if it.plan.turn_relation == "follow_up" and not _has_context(L.state):
            it.plan = it.plan.model_copy(update={"task_type": "clarify", "steps": [], "clarification": (
                ProposedClarification(key="referent", question=FOLLOW_UP_WITHOUT_CONTEXT, options=[]))})
    plan, start = _expand_versions(ctx, it.plan, L.state)
    new_state, effects = apply_turn(start, plan, question=it.question if it.mode not in ("button", "edit") else None)
    cleared = list(effects.cleared)
    deadline = started + get_settings().turn_deadline_seconds - DEADLINE_MARGIN_SECONDS
    run = _Run(ctx, L, it, plan, new_state, question_id, deadline)
    if it.status is not None:
        run.statuses.append(it.status)
    try:
        run.state.query_conditions()
    except ValidationError:
        answer = _finish(clarification_answer("referent", BAD_YEARS, []), L, it, [], None, [], False)
        return TurnResult(answer, None, None, [], {}, None, route, None)

    answer: dict | None = None
    cached = False
    if effects.clarification is None and plan.turn_relation in META_RELATIONS:
        plan_record["meta"] = True
        answer = _meta(run, plan.turn_relation == "meta_why")
    elif effects.clarification is None and plan.task_type == "abstain":
        answer = _unknown_place(run)
    elif effects.clarification is None:
        run.attribute = _plan_attribute(run)
        answer = _cached(run)
        cached = answer is not None
        if not cached:
            answer = _execute_steps(run, _default_steps(plan))
            if (run.clarification is None and it.mode == "model" and _empty(answer)
                    and len(run.steps) < MAX_STEPS and (replanned := _replan(run)) is not None):
                run.plan = plan.model_copy(update={"search_queries": replanned.search_queries or plan.search_queries})
                retry = _execute_steps(run, replanned.steps[:MAX_STEPS - len(run.steps)])
                answer = retry if not _empty(retry) else answer
                plan_record["replan"] = replanned.model_dump(mode="json")
    clarification = effects.clarification or run.clarification
    if clarification is not None:
        state = run.state
        if run.clarification is not None:  # a tool found a result-changing gap
            state = state.model_copy(update={"task_type": "clarify", "pending": PendingClarification(
                key=clarification.key, question=clarification.question, options=clarification.options,
                original_question=it.question, task_type=plan.task_type, topic=state.topic,
                entities=state.entities, conditions=state.conditions, attribute=state.attribute,
                metric=state.metric, unit=state.unit)})
        answer = clarification_answer(clarification.key, clarification.question,
                                      [o.model_dump() for o in clarification.options])
        run.state = state
    if answer is None:
        answer = _unknown_place(run)
    refs = [SourceRef(document_id=str(s["document_id"]), version_id=str(s["version_id"]),
                      page=(s.get("page_list") or [None])[0]) for s in answer.get("sources", [])]
    run.state, _handles = remember_sources(run.state, refs)
    for key in it.removed:  # chip removals the condition delta cannot express
        if getattr(run.state, key) is not None:
            run.state = run.state.model_copy(update={key: None})
            cleared.append(key)
    note = _note(L, it, run.state, effects.resolved)
    answer = _finish(answer, L, it, run.steps, note, _cleared_items(L.state, cleared), run.partial,
                     run.state, run.limitations + _failure_notes(run.statuses, L.pstate), run.incomplete)
    meta = plan_record["meta"]
    key = payload = None
    if (not cached and not meta and clarification is None and plan.task_type != "abstain"
            and answer["kind"] in CACHEABLE_KINDS and run.cacheable and not run.partial and not run.incomplete
            and all(s == CallStatus.OK.value for s in run.statuses) and it.status in (None, CallStatus.OK.value)
            and L.pstate.mode != Mode.ERROR):
        fv = run.facts_versions.get(str(run.attribute.id)) if run.attribute is not None else None
        key = cache_key(run, fv)
        payload = {"answer": {k: v for k, v in answer.items() if k not in PER_TURN_FIELDS},
                   "steps": run.steps, "facts_versions": run.facts_versions}
    conditions = run.state.query_conditions(_intent(plan.task_type) or "calculation")
    return TurnResult(answer, run.state, plan_record, run.steps, run.facts_versions,
                      _intent(plan.task_type), route + ("+cache" if cached else ""),
                      conditions.model_dump(), cache_key=key, cache_payload=payload)


def _failure_notes(statuses: list[str], pstate: ProviderState) -> list[str]:
    """A visible limitation per distinct provider failure in this turn (never hidden behind a demo answer)."""
    out = []
    for st in dict.fromkeys(statuses):
        if st in (CallStatus.OK.value, CallStatus.UNSUPPORTED.value):
            continue
        reason = FAILURE_REASONS.get(st, FAILURE_REASONS[CallStatus.ERROR])
        out.append(f"{reason} ({pstate.provider_name}); חלק מהתשובה הורכב ללא המודל.")
    return out


def _finish(answer: dict, L: Loaded, it: Interpreted, steps: list[dict], note: str | None, cleared: list[dict],
            partial: bool, state: ConversationState | None = None, extra: list[str] | None = None,
            incomplete: bool = False) -> dict:
    """Every answer carries the same keys (docs/api-contract.md)."""
    out = dict(answer)
    limitations = list(out.get("limitations") or [])
    for lim in [*(extra or []), it.limitation, PARTIAL_NOTE if partial else None]:
        if lim and lim not in limitations:
            limitations.append(lim)
    out["limitations"] = limitations
    out.setdefault("numeric", None)
    out.setdefault("claims", [])
    out.setdefault("abstention_kind", None)
    out.setdefault("dropped_claims", 0)
    out.setdefault("preliminary", None)
    out.setdefault("pending_extraction", 0)
    out["mode"] = L.pstate.mode.value
    out["partial"] = partial or incomplete
    out["method"] = out.get("method") or _method(steps)
    out["conditions"] = state.query_conditions().describe() if state is not None else []
    out["interpretation_note"] = note
    out["cleared"] = cleared
    return out
