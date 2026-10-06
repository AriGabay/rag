"""Record tools and clarification helpers for the turn orchestrator (``turn``; U8, U9, KTD2, R12, R20).

- ``load_gazetteer``: the office's places, from verified-or-pending records and from report headers;
- ``clarification_options`` / ``clarification_answer``: the clarifications that change a record result
  (data kind, date field, mixed area, property-type or VAT bases);
- ``numeric_answer``: the price-per-m² answer over unique verified records (no model call).
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
from app.answering.metadata import header_places
from app.answering.parser import Gazetteer
from app.answering.templates import abstain_text, numeric_text
from app.appraisal.query import compute_stats, conflicts, distinct_values, sources_for, uncertain_duplicates
from app.platform.documents import source_file_url

CLARIFY_QUESTIONS = {
    "data_kind": "לאיזה נתון הכוונה?",
    "date_field": "לפי איזה תאריך לסנן את השנה?",
    "area_type": "ברשומות התואמות יש כמה בסיסי שטח, ואין לערבב אותם בממוצע. לפי איזה בסיס שטח לחשב?",
    "property_type": "ברשומות התואמות יש כמה סוגי נכסים. לאיזה סוג נכס לחשב?",
    "vat_basis": "ברשומות התואמות יש בסיסי מע״מ שונים. לפי איזה בסיס לחשב?",
    "filter_conflict": "התנאי בשאלה שונה מהסינון שנבחר. לפי מה לחשב?",
}
# A record attribute that is not money (area, rooms) asks which records only when the choice changes the result.
RECORD_KIND_LABELS = {"transaction_price": "עסקאות (נתוני השוואה)", "appraised_value": "הנכסים הנישומים בשומות"}
MIXED_KEYS = ("area_type", "property_type", "vat_basis")
_MIXED_LABELS = {"area_type": AREA_TYPE_LABELS, "property_type": PROPERTY_TYPE_LABELS, "vat_basis": VAT_LABELS}


@dataclass
class Outcome:
    """A tool answer with the conditions it used (kept for ``content.answer_content``)."""

    answer: dict
    conditions: QueryConditions | None
    intent: str | None
    parse_route: str | None
    pending: dict | None = None
    source_rows: list[dict] = field(default_factory=list)
    cacheable: bool = True  # False when a transient failure degraded the answer


def load_gazetteer(conn: Connection) -> Gazetteer:
    """Places of the office's records plus the places that narrative reports name in their headers."""
    rows = conn.execute(
        text(
            "SELECT DISTINCT o.city, o.neighborhood FROM occurrences o"
            " JOIN document_versions v ON v.id = o.version_id AND v.is_current"
            " JOIN documents d ON d.id = o.document_id AND d.deleted_at IS NULL"
            " WHERE o.verification_status <> 'rejected'"
        )
    ).all()
    cities = {r.city for r in rows if r.city}
    hoods = {(r.city, r.neighborhood) for r in rows if r.neighborhood}
    for city, place in header_places(conn):
        if city is None:
            cities.add(place)
        else:
            hoods.add((city, place))
    return Gazetteer(cities=sorted(cities), neighborhoods=sorted(hoods, key=lambda x: (x[1], x[0] or "")))


def clarification_options(conn: Connection, key: str, c: QueryConditions, *, monetary: bool = True) -> list[dict]:
    if key == "data_kind":
        labels = DATA_KIND_LABELS if monetary else RECORD_KIND_LABELS
        return [{"value": k, "label": labels[k]} for k in ("transaction_price", "appraised_value")]
    if key == "date_field":
        fields = ("transaction_date", "valuation_date") if c.data_kind == "transaction_price" else (
            "valuation_date", "report_date")
        labels = dict(DATE_FIELD_LABELS)
        if c.data_kind == "transaction_price":
            labels["valuation_date"] = "המועד הקובע של השומה שבה הופיעה העסקה"
        return [{"value": f, "label": labels[f]} for f in fields]
    values = distinct_values(conn, c, key)
    return [{"value": v, "label": f"{_MIXED_LABELS[key].get(v, v)} ({n} רשומות)"} for v, n in values]


def clarification_answer(key: str, question: str, options: list[dict]) -> dict:
    """The answer of a clarification turn (``key`` as the client sends it back)."""
    return {"kind": "clarification", "text": question, "provider": "template", "demo": False,
            "clarification": {"key": key, "question": question, "options": options},
            "sources": [], "coverage": None, "limitations": []}


def source_json_rows(rows: list[dict]) -> list[dict]:
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


def numeric_answer(conn: Connection, c: QueryConditions) -> tuple[dict, list[dict]]:
    """The price-per-m² answer over unique verified records (the caller already asked every result-changing
    clarification), with its source rows."""
    stats = compute_stats(conn, c)
    cov = coverage(conn, c)
    if stats.count == 0:
        answer = {"kind": "abstain", "text": abstain_text(c, cov["records_awaiting_verification"]),
                  "provider": "template", "demo": False, "numeric": None, "sources": [], "coverage": cov,
                  "limitations": ["לא קיים בסיס מספיק במאגר המשרד; לא הוצג מספר."]}
        return answer, []
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
        "sources": source_json_rows(rows), "coverage": cov, "limitations": limitations,
    }
    return answer, rows


def mixed_basis(conn: Connection, c: QueryConditions) -> str | None:
    """The first record basis (area, property type, VAT) whose matching records disagree (R12)."""
    for key in MIXED_KEYS:
        if getattr(c, key) is None and len(distinct_values(conn, c, key)) > 1:
            return key
    return None


def _d(value):
    return None if value is None else str(value)
