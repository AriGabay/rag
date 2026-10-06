"""Facts review: list, approve, reject and correct extracted facts (U11, KTD15, KTD9, R16).

Mirrors the record review (``app.appraisal.review``): whoever can see a fact may review it. Every read
and write runs under the reviewer's own context, so RLS hides facts of documents outside the
reviewer's groups (acting on one is a 404). Conflicting values for the same entity are computed at
read time under the same RLS (KTD8 step 6): a value in a document the reviewer cannot see is never
shown, not even as a flag.

Every change appends the prior state to the row's ``previous`` history, bumps only that attribute's
``facts_version`` (so cached answers built on the old facts are not reused) and writes an audit event.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Connection, text

from app.answering import units
from app.answering.attributes import EXTRACTION_PROMPT_VERSION, bump_facts_version, canonical_unit_for
from app.audit import audit
from app.db import TenantContext, tenant_tx
from app.deps import NOT_FOUND, get_ctx, parse_uuid
from app.extraction.normalize_text import base_normalize
from app.platform.documents import source_file_url

router = APIRouter(prefix="/api/review/facts", tags=["review"])

REVIEWABLE = ("needs_review", "auto_validated")
NO_UNIT_LABEL = "ללא יחידה"
# Measurement units the correction form offers. Each label goes through ``units.parse_unit``, so the form
# offers only units the server itself parses and converts; the options shown are those of the attribute's
# dimension. Counts take a plain number (``units.UNITLESS_OK``): a counted noun is not a unit.
UNIT_LABELS = ("מ״ר", "דונם", "סמ״ר", "קמ״ר", "מטר", "ס״מ", "מ״מ", "ק״מ", "מ״ק", "ליטר", "₪", "אלף ₪", "מיליון ₪",
               "₪ למ״ר", "%", "חודשים", "שנים")
_UNITS: dict[str, tuple[str, units.Unit]] = {}
for _label in UNIT_LABELS:
    _unit = units.parse_unit(_label)
    if _unit is not None and _unit.code not in _UNITS:
        _UNITS[_unit.code] = (_label, _unit)
_NUMBER = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")

MSG_NOTE = "יש לתעד את סיבת הדחייה"
MSG_NUMBER = "יש להזין מספר תקין (לדוגמה 12 או 12.5)"
MSG_UNIT = "יש לבחור יחידה המתאימה למאפיין זה"
MSG_NOT_NUMERIC = "ניתן לתקן כאן רק ערכים מספריים"

_SELECT = (
    "SELECT f.id, f.document_id, f.version_id, f.attribute_id, f.entity_role, f.entity_key, f.entity_descriptor,"
    " f.value_numeric, f.value_text, f.unit, f.canonical_value, f.quote, f.source_path, f.status, f.review_note,"
    " f.reviewed_at, f.previous, f.created_at, d.title, a.label_he, a.value_type, a.unit_dimension,"
    " a.canonical_unit, a.facts_version FROM facts f"
    " JOIN document_versions v ON v.id = f.version_id AND v.is_current"
    " JOIN documents d ON d.id = f.document_id AND d.deleted_at IS NULL"
    " JOIN attribute_definitions a ON a.id = f.attribute_id"
    " WHERE f.extraction_version = COALESCE(a.extraction_prompt_version, :ev)"
)


class NoteBody(BaseModel):
    note: str | None = None


class CorrectBody(BaseModel):
    value: str
    unit: str = ""
    note: str | None = None


def _num(value: Decimal | None) -> str | None:
    """Decimal without trailing zeros or exponent ("14.5000" -> "14.5", "1E+3" -> "1000")."""
    return None if value is None else format(value.normalize(), "f")


def unit_options(dimension: str | None) -> list[dict]:
    """Units a correction may use: the attribute's dimension only, canonical first; plus "no unit" where
    a plain number is valid (``units.UNITLESS_OK`` or an attribute without a dimension)."""
    canonical = canonical_unit_for(dimension)
    opts = [{"code": code, "label": label} for code, (label, unit) in _UNITS.items()
            if dimension is not None and unit.dimension == dimension]
    opts.sort(key=lambda o: o["code"] != canonical)
    if dimension is None or dimension in units.UNITLESS_OK:
        opts.append({"code": "", "label": NO_UNIT_LABEL})
    return opts


def _unit_label(code: str | None) -> str | None:
    return _UNITS[code][0] if code in _UNITS else None


def _value(r) -> str | None:
    return _num(r.canonical_value) if r.canonical_value is not None else r.value_text


def _value_key(r):
    return r.canonical_value if r.canonical_value is not None else " ".join(base_normalize(r.value_text or "").split())


def _entity(r) -> str:
    return r.entity_key or f"doc:{r.document_id}"


def _page(r) -> int | None:
    return (r.source_path or {}).get("page")


def _canonical(r) -> str | None:
    return r.canonical_unit or canonical_unit_for(r.unit_dimension)


def _brief(r) -> dict:
    """A fact as one row: value in the canonical unit, verbatim quote and the page link."""
    canonical = _canonical(r)
    return {
        "id": str(r.id), "status": r.status, "value": _value(r), "unit": canonical, "unit_label": _unit_label(canonical),
        "original": {"value_text": r.value_text, "unit": r.unit, "unit_label": _unit_label(r.unit) or r.unit},
        "quote": r.quote, "page": _page(r), "url": source_file_url(r.document_id, r.version_id, _page(r)),
        "document": {"id": str(r.document_id), "title": r.title}, "version_id": str(r.version_id),
    }


def _fact(r, pool: list) -> dict:
    """A fact with its entity, review state and the conflicting values visible to the reader."""
    key, mine = _entity(r), _value_key(r)
    conflicts = [_brief(o) for o in pool
                 if o.id != r.id and o.attribute_id == r.attribute_id and _entity(o) == key and _value_key(o) != mine]
    return {**_brief(r), "entity_role": r.entity_role, "entity_descriptor": r.entity_descriptor,
            "review_note": r.review_note, "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            "previous": r.previous or [], "conflicts": conflicts}


def _attribute(r) -> dict:
    canonical = _canonical(r)
    return {"id": str(r.attribute_id), "label": r.label_he, "value_type": r.value_type,
            "unit_dimension": r.unit_dimension, "canonical_unit": canonical, "canonical_unit_label": _unit_label(canonical),
            "unit_options": unit_options(r.unit_dimension) if r.value_type == "numeric" else [],
            "facts_version": r.facts_version}


def _pool(conn: Connection, attribute_ids: set[UUID]) -> list:
    """Visible, non-rejected facts of current versions for the attributes: the read-time conflict set."""
    if not attribute_ids:
        return []
    return conn.execute(text(_SELECT + " AND f.status <> 'rejected' AND f.attribute_id = ANY(:a)"),
                        {"ev": EXTRACTION_PROMPT_VERSION, "a": list(attribute_ids)}).all()


@router.get("")
def list_facts(ctx: TenantContext = Depends(get_ctx)) -> dict:
    """Facts awaiting review, grouped by attribute then document, ``needs_review`` first."""
    with tenant_tx(ctx) as conn:
        rows = conn.execute(text(_SELECT + " AND f.status IN ('needs_review', 'auto_validated')"),
                            {"ev": EXTRACTION_PROMPT_VERSION}).all()
        pool = _pool(conn, {r.attribute_id for r in rows})
    first = {r.attribute_id for r in rows if r.status == "needs_review"}
    first_docs = {(r.attribute_id, r.document_id) for r in rows if r.status == "needs_review"}
    rows = sorted(rows, key=lambda r: (r.attribute_id not in first, r.label_he, str(r.attribute_id),
                                       (r.attribute_id, r.document_id) not in first_docs, r.title, str(r.document_id),
                                       r.status != "needs_review", r.created_at, str(r.id)))
    groups: dict[UUID, dict] = {}
    for r in rows:
        group = groups.setdefault(r.attribute_id, {"attribute": _attribute(r), "documents": {}})
        doc = group["documents"].setdefault(r.document_id, {
            "document": {"id": str(r.document_id), "title": r.title}, "version_id": str(r.version_id), "facts": []})
        doc["facts"].append(_fact(r, pool))
    return {"attributes": [{**g, "documents": list(g["documents"].values())} for g in groups.values()]}


def _load(conn: Connection, fact_id: UUID, *, lock: bool = False):
    return conn.execute(text(_SELECT + " AND f.id = :f" + (" FOR UPDATE OF f" if lock else "")),
                        {"ev": EXTRACTION_PROMPT_VERSION, "f": fact_id}).one_or_none()


def _detail(conn: Connection, fact_id: UUID) -> dict:
    r = _load(conn, fact_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return {**_fact(r, _pool(conn, {r.attribute_id})), "attribute": _attribute(r), "facts_version": r.facts_version}


@router.get("/{fact_id}")
def fact(fact_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    fid = parse_uuid(fact_id)
    with tenant_tx(ctx) as conn:
        return _detail(conn, fid)


def _history(r, action: str, ctx: TenantContext, **extra) -> str:
    entry = {"action": action, "status": r.status, "at": datetime.now(UTC).isoformat(), "by": str(ctx.user_id), **extra}
    return json.dumps([entry], ensure_ascii=False, default=str)


def _change(fact_id: str, ctx: TenantContext, action: str, apply) -> dict:
    """Load the fact under the reviewer's RLS (404 when invisible), apply the change, bump the attribute's
    ``facts_version`` and audit, all in one transaction."""
    fid = parse_uuid(fact_id)
    with tenant_tx(ctx) as conn:
        r = _load(conn, fid, lock=True)
        if r is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        details = apply(conn, r) or {}
        bump_facts_version(conn, r.attribute_id)
        audit(conn, action, ctx.user_id, "fact", fid, attribute_id=r.attribute_id, **details)
    with tenant_tx(ctx) as conn:
        return _detail(conn, fid)


def _set_status(fact_id: str, new_status: str, note: str | None, ctx: TenantContext, action: str) -> dict:
    def apply(conn: Connection, r) -> dict:
        conn.execute(
            text("UPDATE facts SET status = :s, reviewed_by = :u, reviewed_at = now(),"
                 " review_note = COALESCE(:n, review_note), previous = previous || CAST(:p AS jsonb),"
                 " updated_at = now() WHERE id = :f"),
            {"s": new_status, "u": ctx.user_id, "n": note, "p": _history(r, action.removeprefix("fact_"), ctx),
             "f": r.id})
        return {"note": bool(note)}
    return _change(fact_id, ctx, action, apply)


@router.post("/{fact_id}/approve")
def approve(fact_id: str, body: NoteBody | None = None, ctx: TenantContext = Depends(get_ctx)) -> dict:
    note = (body.note or "").strip() if body else ""
    return _set_status(fact_id, "verified", note or None, ctx, "fact_approve")


@router.post("/{fact_id}/reject")
def reject(fact_id: str, body: NoteBody, ctx: TenantContext = Depends(get_ctx)) -> dict:
    note = (body.note or "").strip()
    if not note:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_NOTE)
    return _set_status(fact_id, "rejected", note, ctx, "fact_reject")


def parse_value(raw: str) -> Decimal:
    """A non-negative number as typed (thousands separators allowed); anything else is a Hebrew 422."""
    s = (raw or "").strip()
    if not _NUMBER.fullmatch(s):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_NUMBER)
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation as exc:  # pragma: no cover - the pattern admits only decimals
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_NUMBER) from exc


def parse_correction(value: str, unit_code: str, value_type: str, dimension: str | None) -> tuple[Decimal, str | None,
                                                                                                    Decimal]:
    """(value, unit code or None, canonical value) for a correction within the attribute's dimension."""
    if value_type != "numeric":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_NOT_NUMERIC)
    number = parse_value(value)
    code = (unit_code or "").strip()
    if code not in {o["code"] for o in unit_options(dimension)}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_UNIT)
    unit = _UNITS[code][1] if code else None
    try:
        conv = units.convert(number, unit, dimension)
    except units.DimensionMismatch as exc:  # pragma: no cover - the options are limited to the dimension
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_UNIT) from exc
    return number, unit.code if unit else None, conv.value


@router.post("/{fact_id}/correct")
def correct(fact_id: str, body: CorrectBody, ctx: TenantContext = Depends(get_ctx)) -> dict:
    note = (body.note or "").strip() or None

    def apply(conn: Connection, r) -> dict:
        number, unit, canonical = parse_correction(body.value, body.unit, r.value_type, r.unit_dimension)
        prior = _history(r, "correct", ctx, value_numeric=_num(r.value_numeric), value_text=r.value_text, unit=r.unit,
                         canonical_value=_num(r.canonical_value), quote=r.quote, source_path=r.source_path)
        conn.execute(
            text("UPDATE facts SET value_numeric = :v, value_text = :vt, unit = :unit, canonical_value = :c,"
                 " status = 'corrected', reviewed_by = :u, reviewed_at = now(), review_note = COALESCE(:n, review_note),"
                 " previous = previous || CAST(:p AS jsonb), updated_at = now() WHERE id = :f"),
            {"v": number, "vt": body.value.strip(), "unit": unit, "c": canonical, "u": ctx.user_id, "n": note,
             "p": prior, "f": r.id})
        return {"unit": unit, "note": bool(note)}
    return _change(fact_id, ctx, "fact_correct", apply)
