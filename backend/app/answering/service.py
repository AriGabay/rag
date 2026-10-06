"""Question answering orchestration (U8, U10, U11).

calculation -> clarify missing conditions -> parametric SQL -> template (no model call)
content     -> hybrid retrieval over authorized chunks -> model answer (if enabled) or extractive
combined    -> calculation first, then explanations retrieved under the same place/date conditions
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import ValidationError
from sqlalchemy import Connection, text

from app.answering.conditions import (
    AREA_TYPE_LABELS,
    DATA_KIND_LABELS,
    DATE_FIELD_LABELS,
    PROPERTY_TYPE_LABELS,
    VAT_LABELS,
    QueryConditions,
)
from app.answering.coverage import coverage
from app.answering.parser import Gazetteer, ParseResult, missing_conditions, parse_question
from app.answering.templates import abstain_text, numeric_text
from app.appraisal.query import compute_stats, conflicts, distinct_values, sources_for, uncertain_duplicates
from app.db import TenantContext
from app.platform.documents import source_file_url

CLARIFY_QUESTIONS = {
    "data_kind": "לאיזה נתון הכוונה?",
    "date_field": "לפי איזה תאריך לסנן את השנה?",
    "area_type": "ברשומות התואמות יש כמה בסיסי שטח, ואין לערבב אותם בממוצע. לפי איזה בסיס שטח לחשב?",
    "property_type": "ברשומות התואמות יש כמה סוגי נכסים. לאיזה סוג נכס לחשב?",
    "vat_basis": "ברשומות התואמות יש בסיסי מע״מ שונים. לפי איזה בסיס לחשב?",
    "filter_conflict": "התנאי בשאלה שונה מהסינון שנבחר. לפי מה לחשב?",
}
MIXED_KEYS = ("area_type", "property_type", "vat_basis")
_MIXED_LABELS = {"area_type": AREA_TYPE_LABELS, "property_type": PROPERTY_TYPE_LABELS, "vat_basis": VAT_LABELS}


@dataclass
class Outcome:
    answer: dict
    conditions: QueryConditions | None
    intent: str | None
    parse_route: str | None
    pending: dict | None = None
    source_rows: list[dict] = field(default_factory=list)
    cacheable: bool = True  # False when a transient failure degraded the answer


def load_gazetteer(conn: Connection) -> Gazetteer:
    rows = conn.execute(
        text(
            "SELECT DISTINCT o.city, o.neighborhood FROM occurrences o"
            " JOIN document_versions v ON v.id = o.version_id AND v.is_current"
            " JOIN documents d ON d.id = o.document_id AND d.deleted_at IS NULL"
            " WHERE o.verification_status <> 'rejected'"
        )
    ).all()
    cities = sorted({r.city for r in rows if r.city})
    hoods = sorted({(r.city, r.neighborhood) for r in rows if r.neighborhood}, key=lambda x: (x[1], x[0] or ""))
    return Gazetteer(cities=cities, neighborhoods=hoods)


def clarification_options(conn: Connection, key: str, c: QueryConditions) -> list[dict]:
    if key == "data_kind":
        return [{"value": k, "label": DATA_KIND_LABELS[k]} for k in ("transaction_price", "appraised_value")]
    if key == "date_field":
        fields = ("transaction_date", "valuation_date") if c.data_kind == "transaction_price" else (
            "valuation_date", "report_date")
        labels = dict(DATE_FIELD_LABELS)
        if c.data_kind == "transaction_price":
            labels["valuation_date"] = "המועד הקובע של השומה שבה הופיעה העסקה"
        return [{"value": f, "label": labels[f]} for f in fields]
    values = distinct_values(conn, c, key)
    return [{"value": v, "label": f"{_MIXED_LABELS[key].get(v, v)} ({n} רשומות)"} for v, n in values]


def clarification(conn: Connection, key: str, c: QueryConditions, question: str, route: str) -> Outcome:
    return clarification_outcome(key, clarification_options(conn, key, c), c, question, route)


def clarification_outcome(key: str, options: list[dict], c: QueryConditions, question: str, route: str) -> Outcome:
    """The clarification answer and the pending state the next turn resumes from."""
    answer = {
        "kind": "clarification", "text": CLARIFY_QUESTIONS[key], "provider": "template", "demo": False,
        "clarification": {"key": key, "question": CLARIFY_QUESTIONS[key], "options": options},
        "sources": [], "coverage": None, "limitations": [],
    }
    pending = {"key": key, "question": CLARIFY_QUESTIONS[key], "options": options,
               "conditions": c.model_dump(), "original_question": question, "route": route}
    return Outcome(answer, c, c.intent, route, pending=pending)


def _source_json(rows: list[dict]) -> list[dict]:
    out = []
    for i, r in enumerate(rows, start=1):
        page = r.get("page_no") if r.get("mime_type", "application/pdf") == "application/pdf" else None
        out.append({
            "evidence_id": f"E{i}", "document_id": str(r["document_id"]), "version_id": str(r["version_id"]),
            "title": r["title"], "page_list": [page] if page else [], "section": r.get("section"),
            "row": r.get("row_index"), "snippet": r.get("text_span") or r.get("snippet"),
            "url": source_file_url(r["document_id"], r["version_id"], page),
        })
    return out


def answer_numeric(conn: Connection, c: QueryConditions, route: str, question: str) -> Outcome:
    for key in MIXED_KEYS:
        if getattr(c, key) is None and len(distinct_values(conn, c, key)) > 1:
            return clarification(conn, key, c, question, route)
    stats = compute_stats(conn, c)
    cov = coverage(conn, c)
    if stats.count == 0:
        answer = {"kind": "abstain", "text": abstain_text(c, cov["records_awaiting_verification"]),
                  "provider": "template", "demo": False, "numeric": None, "sources": [], "coverage": cov,
                  "limitations": ["לא קיים בסיס מספיק במאגר המשרד; לא הוצג מספר."]}
        return Outcome(answer, c, c.intent, route)
    rows = sources_for(conn, stats.transaction_ids)
    uncertain = uncertain_duplicates(conn, stats.transaction_ids)
    conflict_count = conflicts(conn, stats.transaction_ids)
    body, limitations = numeric_text(c, stats, uncertain, conflict_count)
    answer = {
        "kind": "numeric", "text": body, "provider": "template", "demo": False,
        "numeric": {
            "conditions": c.describe(), "record_count": stats.count,
            "mean_price_per_sqm": _d(stats.mean), "weighted_price_per_sqm": _d(stats.weighted),
            "median_price_per_sqm": _d(stats.median) if stats.count >= 3 else None,
            "min_price_per_sqm": _d(stats.minimum), "max_price_per_sqm": _d(stats.maximum),
            "currency": "ILS", "uncertain_duplicates": uncertain, "conflicts": conflict_count,
        },
        "sources": _source_json(rows), "coverage": cov, "limitations": limitations,
    }
    return Outcome(answer, c, c.intent, route, source_rows=rows)


def _d(value):
    return None if value is None else str(value)


def _merge_filters(parsed: ParseResult, filters: dict | None) -> tuple[QueryConditions, str | None]:
    """Filters fill conditions the question left open. A filter that contradicts a value the user
    stated in this turn (not one inherited from earlier turns) returns that key for clarification."""
    c = parsed.conditions
    if not filters:
        return c, None
    update = {}
    for key in ("city", "neighborhood", "data_kind", "date_field", "year_from", "year_to"):
        value = filters.get(key)
        if value in (None, ""):
            continue
        current = getattr(c, key)
        stated_now = parsed.route != "followup" or key in parsed.explicit
        if current is not None and current != value and stated_now:
            return c, key
        update[key] = value
    try:
        return QueryConditions.model_validate(c.model_dump() | update), None
    except ValidationError:  # e.g. a filter year range that ends before it starts
        return c, next(iter(update))


def cloud_parser(conn: Connection):
    """The selected cloud provider, only in ``cloud`` mode (``providers.status``); never the demo mock."""
    from app.answering.content import select_provider
    from app.providers.status import Mode

    provider, state = select_provider(conn)
    return provider if state.mode == Mode.CLOUD else None


def _needs_model(parsed: ParseResult) -> bool:
    c = parsed.conditions
    return (parsed.route == "rules" and c.intent in ("calculation", "combined") and c.data_kind is None
            and c.city is None and c.neighborhood is None and c.year_from is None)


def model_parse(conn: Connection, question: str, gaz: Gazetteer) -> QueryConditions | None:
    provider = cloud_parser(conn)
    if provider is None:
        return None
    try:
        data = provider.parse_conditions(question, QueryConditions.model_json_schema())
        conds = QueryConditions.model_validate(data) if data else None
    except Exception:  # noqa: BLE001 - invalid model output means "could not parse"
        return None
    if conds is None:
        return None
    known_cities = set(gaz.cities)
    known_hoods = {n for _, n in gaz.neighborhoods}
    if (conds.city and conds.city not in known_cities) or (conds.neighborhood and conds.neighborhood not in known_hoods):
        return None  # places must come from the office's own data
    return conds


@dataclass
class AskInput:
    question: str | None
    filters: dict | None
    clarification: dict | None


def run_question(conn: Connection, ctx: TenantContext, inp: AskInput, previous: QueryConditions | None,
                 pending: dict | None, cache=None) -> Outcome:
    """``cache(conditions, question)`` returns a still-valid cached answer or None (checked before any
    SQL computation or model call)."""
    from app.answering.content import answer_content  # imported here: content builds service.Outcome

    if inp.clarification and pending and inp.clarification.get("key") == pending["key"]:
        key, value = pending["key"], inp.clarification.get("value")
        allowed = {o["value"] for o in pending.get("options", [])}
        if value not in allowed:
            return clarification(conn, key, QueryConditions.model_validate(pending["conditions"]),
                                 pending["original_question"], pending["route"])
        c = QueryConditions.model_validate(pending["conditions"])
        if key == "filter_conflict":
            field_name, chosen = value.split(":", 1)
            c = c.model_copy(update={field_name: int(chosen) if field_name.startswith("year") else chosen})
        else:
            c = c.model_copy(update={key: value})
        question, route = pending["original_question"], pending["route"] + "+clarification"
    else:
        question = (inp.question or "").strip()
        gaz = load_gazetteer(conn)
        parsed = parse_question(question, gaz, previous)
        if _needs_model(parsed):
            modeled = model_parse(conn, question, gaz)
            if modeled is not None:
                parsed = ParseResult(modeled, missing_conditions(modeled), "model")
        if parsed.unknown_place:
            cov = coverage(conn, None)
            answer = {"kind": "abstain", "provider": "template", "demo": False, "sources": [], "coverage": cov,
                      "text": f"אין במאגר המשרד רשומות עבור \"{parsed.unknown_place}\". לא חושב מספר.",
                      "limitations": ["המערכת עונה רק על סמך מסמכי המשרד ואינה משלימה מידע ממקורות אחרים."]}
            return Outcome(answer, None, parsed.conditions.intent, parsed.route)  # never cached
        c, conflict_key = _merge_filters(parsed, inp.filters)
        route = parsed.route
        if conflict_key:
            options = [{"value": f"{conflict_key}:{getattr(c, conflict_key)}", "label": f"לפי השאלה: {getattr(c, conflict_key)}"},
                       {"value": f"{conflict_key}:{inp.filters[conflict_key]}", "label": f"לפי הסינון: {inp.filters[conflict_key]}"}]
            return clarification_outcome("filter_conflict", options, c, question, route)

    missing = missing_conditions(c)
    if missing:
        return clarification(conn, missing[0], c, question, route)
    if cache is not None:
        hit = cache(c, question)
        if hit is not None:
            return Outcome(hit, c, c.intent, route + "+cache")
    if c.intent in ("explanation", "document_lookup"):
        return answer_content(conn, ctx, question, c, route, numeric=None)
    numeric = answer_numeric(conn, c, route, question)
    if c.intent == "combined" and numeric.answer["kind"] in ("numeric", "abstain"):
        return answer_content(conn, ctx, question, c, route, numeric=numeric)
    return numeric

