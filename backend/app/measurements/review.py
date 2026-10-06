"""Measurement review: list, verify, correct and reject measurements, and settle a re-extraction conflict.

Whoever can see a measurement may review it (RLS on its document decides). Every change keeps the prior state
in ``previous``, writes an audit event and moves the office's data version, so answers built on the earlier
state are shown as outdated. A change carries the status the reviewer saw: if the row changed meanwhile it is
a 409 and nothing is written (an action on a stale list never overwrites someone else's review).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text

from app.audit import audit
from app.chat.tools import KIND_LABELS, ROLE_LABELS, SUBJECT_ROLE_LABELS
from app.db import TenantContext, bump_data_version, tenant_tx
from app.deps import NOT_FOUND, get_ctx, parse_uuid
from app.measurements.extract import PERIOD_LABELS, UNIT_LABELS, VAT_LABELS
from app.measurements.values import parse_amount

router = APIRouter(prefix="/api/review/measurements", tags=["review"])

STATUS_LABELS = {"auto_validated": "ראשוני", "needs_review": "ממתין לבדיקה", "verified": "אומת", "corrected": "תוקן",
                 "rejected": "נדחה"}
FORM_LABELS = {"exact": "מדויק", "approximate": "מקורב", "range": "טווח", "minimum": "לפחות", "maximum": "לכל היותר"}
MSG_STALE = "הנתון השתנה בינתיים; טענו את הרשימה מחדש"
MSG_NOTE = "יש לתעד את סיבת הדחייה"
MSG_VALUE = "יש להזין ערך כפי שנכתב במסמך, עם מספר"
PAGE = 100

_SELECT = ("SELECT m.*, d.title FROM measurements m JOIN document_versions v ON v.id = m.version_id"
           " JOIN documents d ON d.id = m.document_id AND d.deleted_at IS NULL")


def _json(r) -> dict:
    return {
        "id": str(r.id), "document_id": str(r.document_id), "version_id": str(r.version_id), "title": r.title,
        "metric": r.metric, "metric_kind": r.metric_kind, "metric_kind_label": KIND_LABELS.get(r.metric_kind, r.metric_kind),
        "value_text": r.value_text, "value": None if r.value is None else str(r.value),
        "value_low": None if r.value_low is None else str(r.value_low),
        "value_high": None if r.value_high is None else str(r.value_high),
        "value_form": r.value_form, "value_form_label": FORM_LABELS.get(r.value_form, r.value_form),
        "unit": r.unit, "unit_label": UNIT_LABELS.get(r.unit or "", r.unit or ""),
        "period": r.period, "period_label": PERIOD_LABELS.get(r.period, r.period),
        "vat": r.vat, "vat_label": VAT_LABELS.get(r.vat, r.vat) or "לא רלוונטי",
        "area_basis": r.area_basis, "subject": r.subject,
        "subject_role": r.subject_role, "subject_role_label": SUBJECT_ROLE_LABELS.get(r.subject_role, r.subject_role),
        "value_role": r.value_role, "value_role_label": ROLE_LABELS.get(r.value_role, r.value_role),
        "effective_date": r.effective_date, "quote": r.quote, "section": r.section, "block_index": r.block_index,
        "table_index": r.table_index, "row_index": r.row_index, "status": r.status,
        "status_label": STATUS_LABELS.get(r.status, r.status), "issues": r.issues or [], "conflict": r.conflict,
        "review_note": r.review_note, "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
        "previous": r.previous or [], "extraction_version": r.extraction_version,
    }


@router.get("")
def list_measurements(document_id: str | None = None, status_filter: str | None = Query(None, alias="status"),
                      kind: str | None = None, q: str | None = Query(None, max_length=100), offset: int = 0,
                      ctx: TenantContext = Depends(get_ctx)) -> dict:
    conds = ["v.is_current"]
    params: dict = {"o": max(0, offset), "lim": PAGE}
    if document_id:
        conds.append("m.document_id = :d")
        params["d"] = parse_uuid(document_id)
    if status_filter:
        conds.append("m.status = :s")
        params["s"] = status_filter
    else:
        conds.append("m.status <> 'rejected'")
    if kind:
        conds.append("m.metric_kind = :k")
        params["k"] = kind
    if q and q.strip():
        conds.append("(m.metric ILIKE :q OR m.quote ILIKE :q OR m.subject ILIKE :q OR m.value_text ILIKE :q)")
        params["q"] = f"%{q.strip()}%"
    where = " WHERE " + " AND ".join(conds)
    with tenant_tx(ctx) as conn:
        rows = conn.execute(text(_SELECT + where + " ORDER BY (m.conflict IS NULL), (m.status <> 'needs_review'),"
                                 " d.title, m.block_index, m.row_index NULLS FIRST, m.created_at OFFSET :o LIMIT :lim"),
                            params).all()
        total = conn.execute(text("SELECT count(*) FROM measurements m JOIN document_versions v ON v.id = m.version_id"
                                  " JOIN documents d ON d.id = m.document_id AND d.deleted_at IS NULL" + where),
                             params).scalar_one()
        counts = dict(conn.execute(text(
            "SELECT m.status, count(*) FROM measurements m JOIN document_versions v ON v.id = m.version_id"
            " JOIN documents d ON d.id = m.document_id AND d.deleted_at IS NULL WHERE v.is_current GROUP BY 1")).all())
    return {"items": [_json(r) for r in rows], "total": total, "counts": counts,
            "kinds": KIND_LABELS, "units": UNIT_LABELS, "periods": PERIOD_LABELS, "vats": VAT_LABELS}


class Review(BaseModel):
    expected_status: str
    note: str | None = Field(default=None, max_length=1000)


class Correction(Review):
    value_text: str | None = Field(default=None, max_length=200)
    unit: str | None = None
    period: Literal["month", "year", "one_time", "none", "unknown"] | None = None
    vat: Literal["included", "excluded", "unknown", "not_applicable"] | None = None
    area_basis: str | None = Field(default=None, max_length=100)
    metric: str | None = Field(default=None, max_length=300)
    metric_kind: str | None = None


def _locked(conn: Connection, mid: UUID, expected: str):
    r = conn.execute(text(_SELECT + " WHERE m.id = :m FOR UPDATE OF m"), {"m": mid}).first()
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if r.status != expected:
        raise HTTPException(status.HTTP_409_CONFLICT, MSG_STALE)
    return r


def _snapshot(r) -> dict:
    return {"status": r.status, "value_text": r.value_text, "value": None if r.value is None else str(r.value),
            "unit": r.unit, "period": r.period, "vat": r.vat, "area_basis": r.area_basis, "metric": r.metric,
            "metric_kind": r.metric_kind, "review_note": r.review_note,
            "at": datetime.now(UTC).isoformat()}


def _write(conn: Connection, ctx: TenantContext, r, changes: dict, new_status: str, note: str | None, action: str):
    sets = ", ".join(f"{k} = :{k}" for k in changes)
    conn.execute(text(
        "UPDATE measurements SET " + (sets + ", " if sets else "") + "status = :st, review_note = :n, reviewed_by = :u,"
        " reviewed_at = now(), updated_at = now(), conflict = NULL,"
        " previous = previous || CAST(:p AS jsonb) WHERE id = :id"),
        changes | {"st": new_status, "n": note, "u": ctx.user_id, "id": r.id,
                   "p": json.dumps([_snapshot(r)], ensure_ascii=False)})
    bump_data_version(conn)
    audit(conn, f"measurement_{action}", ctx.user_id, "measurement", r.id)
    return _json(conn.execute(text(_SELECT + " WHERE m.id = :m"), {"m": r.id}).one())


@router.post("/{measurement_id}/verify")
def verify(measurement_id: str, body: Review, ctx: TenantContext = Depends(get_ctx)) -> dict:
    with tenant_tx(ctx) as conn:
        r = _locked(conn, parse_uuid(measurement_id), body.expected_status)
        return _write(conn, ctx, r, {}, "verified", body.note, "verify")


@router.post("/{measurement_id}/reject")
def reject(measurement_id: str, body: Review, ctx: TenantContext = Depends(get_ctx)) -> dict:
    if not (body.note or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_NOTE)
    with tenant_tx(ctx) as conn:
        r = _locked(conn, parse_uuid(measurement_id), body.expected_status)
        return _write(conn, ctx, r, {}, "rejected", body.note, "reject")


@router.post("/{measurement_id}/correct")
def correct(measurement_id: str, body: Correction, ctx: TenantContext = Depends(get_ctx)) -> dict:
    changes: dict = {}
    if body.value_text is not None:
        amount = parse_amount(body.value_text)
        if amount is None or (amount.value is None and amount.low is None):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_VALUE)
        changes |= {"value_text": body.value_text.strip(), "value": amount.value, "value_low": amount.low,
                    "value_high": amount.high, "value_form": amount.form}
    if body.unit is not None:
        if body.unit not in UNIT_LABELS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "יחידה לא מוכרת")
        changes["unit"] = body.unit
    if body.metric_kind is not None:
        if body.metric_kind not in KIND_LABELS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "סוג מדד לא מוכר")
        changes["metric_kind"] = body.metric_kind
    for k in ("period", "vat", "metric"):
        if getattr(body, k) is not None:
            changes[k] = getattr(body, k)
    if body.area_basis is not None:
        changes["area_basis"] = body.area_basis.strip() or None
    with tenant_tx(ctx) as conn:
        r = _locked(conn, parse_uuid(measurement_id), body.expected_status)
        return _write(conn, ctx, r, changes, "corrected", body.note, "correct")
