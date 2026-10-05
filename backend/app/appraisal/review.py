"""Review queue, record view, approve / correct / reject, dedup resolution (U7).

Authorization runs under the user's own context (RLS). The write itself then runs in the office's
``system`` context because re-attaching a corrected record may touch a transaction whose other
occurrences the user cannot see; the server only does this after checking the user may act."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import Connection, text

from app.appraisal.dedup import TxnFacts, attach_transaction, delete_orphans, find_uncertain, lock_office
from app.appraisal.normalize import normalize_place, parse_area_type, parse_date, parse_decimal, parse_money
from app.appraisal.publish import refresh_version_status
from app.appraisal.validate import CONFLICT_TOLERANCE, CRITICAL, compute_price_per_sqm
from app.audit import audit
from app.db import TenantContext, bump_data_version, tenant_tx
from app.deps import FORBIDDEN, NOT_FOUND, get_ctx, parse_uuid
from app.platform.pipeline import system_ctx

router = APIRouter(prefix="/api/review", tags=["review"])

FIELD_LABELS = {
    "data_kind": "סוג נתון", "city": "עיר", "neighborhood": "שכונה", "address": "כתובת", "block": "גוש",
    "parcel": "חלקה", "sub_parcel": "תת חלקה", "property_type": "סוג נכס", "rooms": "חדרים",
    "transaction_date": "תאריך עסקה", "valuation_date": "המועד הקובע", "report_date": "תאריך עריכת השומה",
    "area": "שטח", "area_type": "סוג שטח", "price": "מחיר / שווי", "currency": "מטבע", "vat_basis": "בסיס מע״מ",
    "price_per_sqm_stated": "מחיר למ״ר (כפי שמופיע במסמך)",
}
TEXT_FIELDS = {"city", "neighborhood", "address", "block", "parcel", "sub_parcel", "property_type", "vat_basis"}
DECIMAL_FIELDS = {"area", "rooms"}
MONEY_FIELDS = {"price", "price_per_sqm_stated"}
DATE_FIELDS = {"transaction_date", "valuation_date", "report_date"}
TXN_FIELDS = {"block", "parcel", "sub_parcel", "address", "property_type", "transaction_date", "price", "area", "area_type"}
DATA_KINDS = {"transaction_price", "appraised_value", "asking_price", "adjusted_comparable"}

MSG_NUMBER = "יש להזין מספר תקין (לדוגמה 1,250,000 או 95.5)"
MSG_DATE = "יש להזין תאריך תקין בפורמט DD/MM/YYYY"
MSG_AREA_TYPE = "סוג שטח לא מוכר (נטו, ברוטו, רשום, אקוויוולנטי)"
MSG_NOTE = "יש לתעד את סיבת התיקון"
MSG_FIELD = "שדה לא ניתן לעריכה"
MSG_KIND = "סוג נתון לא מוכר"


class NoteBody(BaseModel):
    note: str | None = None


class CorrectBody(BaseModel):
    field: str
    value: str
    note: str


def _occurrence(conn: Connection, occ_id: UUID):
    return conn.execute(
        text(
            "SELECT o.*, v.mime_type, v.is_current, d.title FROM occurrences o"
            " JOIN document_versions v ON v.id = o.version_id"
            " JOIN documents d ON d.id = o.document_id AND d.deleted_at IS NULL WHERE o.id = :o"
        ),
        {"o": occ_id},
    ).first()


def _fully_visible(ctx: TenantContext, txn_ids: list[UUID]) -> bool:
    """Non-admins may act only when every occurrence behind the transactions is in their groups."""
    if ctx.is_admin:
        return True
    with tenant_tx(system_ctx(ctx.office_id)) as conn:
        groups = set(
            conn.execute(
                text("SELECT DISTINCT d.group_id FROM occurrences o JOIN documents d ON d.id = o.document_id"
                     " WHERE o.transaction_id = ANY(:t)"),
                {"t": txn_ids},
            ).scalars()
        )
    return groups <= set(ctx.group_ids)


def _summary(row) -> dict:
    return {
        "data_kind": row.data_kind, "city": row.city, "neighborhood": row.neighborhood, "address": row.address,
        "price": _s(row.price), "area": _s(row.area), "area_type": row.area_type,
        "transaction_date": _s(row.transaction_date), "valuation_date": _s(row.valuation_date),
    }


def _s(value):
    if value is None:
        return None
    if isinstance(value, (Decimal, date, datetime)):
        return value.isoformat() if isinstance(value, (date, datetime)) else str(value)
    return value


def _txn_summary(conn: Connection, txn_id: UUID) -> dict:
    t = conn.execute(text("SELECT * FROM transactions WHERE id = :t"), {"t": txn_id}).one()
    occs = conn.execute(
        text("SELECT o.document_id, o.version_id, o.page_no, o.row_index, o.address, d.title FROM occurrences o"
             " JOIN documents d ON d.id = o.document_id WHERE o.transaction_id = :t ORDER BY d.title"),
        {"t": txn_id},
    ).all()
    return {
        "transaction_id": str(txn_id), "data_kind": t.data_kind, "address": occs[0].address if occs else None,
        "price": _s(t.price), "area": _s(t.area), "area_type": t.area_type, "transaction_date": _s(t.transaction_date),
        "sources": [{"document_id": str(o.document_id), "version_id": str(o.version_id), "title": o.title,
                     "page_list": [o.page_no] if o.page_no else [], "row": o.row_index,
                     "url": f"/api/documents/{o.document_id}/versions/{o.version_id}/file#page={o.page_no or 1}"}
                    for o in occs],
    }


@router.get("/queue")
def queue(document_id: str | None = None, ctx: TenantContext = Depends(get_ctx)) -> dict:
    doc = parse_uuid(document_id) if document_id else None
    with tenant_tx(ctx) as conn:
        rows = conn.execute(
            text(
                "SELECT o.*, d.title FROM occurrences o JOIN document_versions v ON v.id = o.version_id AND v.is_current"
                " JOIN documents d ON d.id = o.document_id AND d.deleted_at IS NULL"
                " WHERE o.verification_status IN ('needs_review', 'auto_extracted')"
                " AND (CAST(:d AS uuid) IS NULL OR o.document_id = CAST(:d AS uuid))"
                " ORDER BY (o.verification_status = 'needs_review') DESC, d.title, o.record_index"
            ),
            {"d": doc},
        ).all()
        cands = conn.execute(
            text("SELECT c.* FROM dedup_candidates c WHERE c.status = 'open'"
                 " AND EXISTS (SELECT 1 FROM transactions t WHERE t.id = c.transaction_a)"
                 " AND EXISTS (SELECT 1 FROM transactions t WHERE t.id = c.transaction_b) ORDER BY c.created_at")
        ).all()
        visible_cands = [c for c in cands if _fully_visible(ctx, [c.transaction_a, c.transaction_b])]
        items = [
            {"kind": "dedup", "id": str(c.id), "reason": c.reason, "a": _txn_summary(conn, c.transaction_a),
             "b": _txn_summary(conn, c.transaction_b)}
            for c in visible_cands
        ]
    for r in rows:
        items.append({
            "kind": "record", "id": str(r.id), "document": {"id": str(r.document_id), "title": r.title},
            "version_id": str(r.version_id), "page_no": r.page_no, "verification_status": r.verification_status,
            "summary": _summary(r),
            "flags": {"conflict": r.conflict_flag, "missing_critical": list(r.missing_critical), "ocr": r.ocr},
        })
    return {"items": items}


def _detail(conn: Connection, occ_id: UUID) -> dict:
    o = _occurrence(conn, occ_id)
    if o is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    facts = {f.field: f for f in conn.execute(
        text("SELECT * FROM fact_values WHERE occurrence_id = :o"), {"o": occ_id}).all()}
    calc = facts.get("price_per_sqm_computed")
    fields = []
    for name, label in FIELD_LABELS.items():
        fv = facts.get(name)
        fields.append({
            "field": name, "label": label,
            "original_text": fv.original_text if fv else None,
            "normalized_value": _s(getattr(o, name, None)) if name != "currency" else o.currency,
            "status": fv.status if fv else "missing",
            "source_path": fv.source_path if fv else None,
            "previous": fv.previous if fv else [],
        })
    return {
        "id": str(o.id), "document": {"id": str(o.document_id), "title": o.title}, "version_id": str(o.version_id),
        "page_no": o.page_no, "table_index": o.table_index, "row_index": o.row_index, "text_span": o.text_span,
        "is_docx": o.mime_type != "application/pdf", "verification_status": o.verification_status,
        "conflict_flag": o.conflict_flag, "missing_critical": list(o.missing_critical), "ocr": o.ocr,
        "review_note": o.review_note, "fields": fields, "computed_price_per_sqm": _s(o.price_per_sqm_computed),
        "stated_price_per_sqm": _s(o.price_per_sqm_stated), "calc_definition": calc.original_text if calc else None,
        "file_url": f"/api/documents/{o.document_id}/versions/{o.version_id}/file#page={o.page_no or 1}",
    }


@router.get("/records/{occurrence_id}")
def record(occurrence_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    occ = parse_uuid(occurrence_id)
    with tenant_tx(ctx) as conn:
        o = _occurrence(conn, occ)
        if o is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        if not _fully_visible(ctx, [o.transaction_id]):
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        return _detail(conn, occ)


def _authorize(ctx: TenantContext, occ: UUID):
    with tenant_tx(ctx) as conn:
        o = _occurrence(conn, occ)
    if o is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    if not _fully_visible(ctx, [o.transaction_id]):
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN)
    return o


def _finish(conn: Connection, ctx: TenantContext, version_ids: set[UUID]) -> None:
    for v in version_ids:
        refresh_version_status(conn, v)
    bump_data_version(conn)


def _set_status(occurrence_id: str, new_status: str, note: str | None, ctx: TenantContext, action: str) -> dict:
    occ = parse_uuid(occurrence_id)
    o = _authorize(ctx, occ)
    with tenant_tx(system_ctx(ctx.office_id)) as conn:
        conn.execute(
            text("UPDATE occurrences SET verification_status = :s, verified_by = :u, verified_at = now(),"
                 " review_note = COALESCE(:n, review_note) WHERE id = :o"),
            {"s": new_status, "u": ctx.user_id, "n": note, "o": occ},
        )
        _finish(conn, ctx, {o.version_id})
        audit(conn, action, ctx.user_id, "occurrence", occ, note=bool(note))
    with tenant_tx(ctx) as conn:
        return _detail(conn, occ)


@router.post("/records/{occurrence_id}/approve")
def approve(occurrence_id: str, body: NoteBody | None = None, ctx: TenantContext = Depends(get_ctx)) -> dict:
    return _set_status(occurrence_id, "human_verified", body.note if body else None, ctx, "record_approve")


@router.post("/records/{occurrence_id}/reject")
def reject(occurrence_id: str, body: NoteBody, ctx: TenantContext = Depends(get_ctx)) -> dict:
    if not (body.note or "").strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_NOTE)
    return _set_status(occurrence_id, "rejected", body.note, ctx, "record_reject")


def parse_correction(field: str, value: str):
    raw = value.strip()
    if field == "data_kind":
        if raw not in DATA_KINDS:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_KIND)
        return raw
    if field in TEXT_FIELDS:
        return normalize_place(raw)
    if field in MONEY_FIELDS:
        parsed = parse_money(raw)
    elif field in DECIMAL_FIELDS:
        parsed = parse_decimal(raw)
    elif field in DATE_FIELDS:
        parsed = date.fromisoformat(raw) if len(raw) == 10 and raw[4] == "-" else parse_date(raw)
        if parsed is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_DATE)
        return parsed
    elif field == "area_type":
        parsed = raw if raw in {"net", "gross", "registered", "equivalent", "other"} else parse_area_type(raw)
        if parsed is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_AREA_TYPE)
        return parsed
    else:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_FIELD)
    if parsed is None or parsed < 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_NUMBER)
    return parsed


def reattach(conn: Connection, occ_id: UUID) -> None:
    """Recompute derived values and transaction identity of one occurrence after a correction."""
    o = conn.execute(text("SELECT * FROM occurrences WHERE id = :o"), {"o": occ_id}).one()
    computed = compute_price_per_sqm(o.price, o.area)
    conflict = bool(o.price_per_sqm_stated is not None and computed and
                    abs(o.price_per_sqm_stated - computed) / computed > CONFLICT_TOLERANCE)
    missing = [f for f in CRITICAL.get(o.data_kind, ()) if getattr(o, f) is None]
    definition = f"price / area = {o.price} / {o.area}" if computed is not None else None
    facts = TxnFacts(o.data_kind, o.block, o.parcel, o.sub_parcel, o.address, o.property_type, o.transaction_date,
                     o.valuation_date, o.report_date, o.price, o.area, o.area_type, computed, definition)
    lock_office(conn)
    old_txn = o.transaction_id
    others = conn.execute(text("SELECT count(*) FROM occurrences WHERE transaction_id = :t AND id <> :o"),
                          {"t": old_txn, "o": occ_id}).scalar_one()
    if others == 0:
        conn.execute(text("UPDATE transactions SET match_key = NULL WHERE id = :t"), {"t": old_txn})
    txn_id, _ = attach_transaction(conn, facts)
    conn.execute(
        text("UPDATE occurrences SET transaction_id = :t, price_per_sqm_computed = :c, conflict_flag = :cf,"
             " missing_critical = :m WHERE id = :o"),
        {"t": txn_id, "c": computed, "cf": conflict, "m": missing, "o": occ_id},
    )
    conn.execute(text("DELETE FROM fact_values WHERE occurrence_id = :o AND field = 'price_per_sqm_computed'"),
                 {"o": occ_id})
    if computed is not None:
        conn.execute(
            text("INSERT INTO fact_values (office_id, document_id, occurrence_id, field, original_text,"
                 " normalized_value, source_path, extraction_version, status) VALUES (app_office(), :d, :o,"
                 " 'price_per_sqm_computed', :def, :v, CAST(:sp AS jsonb), :ev, 'computed')"),
            {"d": o.document_id, "o": occ_id, "def": definition, "v": str(computed),
             "sp": json.dumps({"lineage": ["price", "area"], "version_id": str(o.version_id)}),
             "ev": o.extraction_version},
        )
    delete_orphans(conn)
    find_uncertain(conn, txn_id, facts, o.version_id)


@router.post("/records/{occurrence_id}/correct")
def correct(occurrence_id: str, body: CorrectBody, ctx: TenantContext = Depends(get_ctx)) -> dict:
    if not body.note.strip():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_NOTE)
    if body.field not in FIELD_LABELS or body.field == "currency":
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_FIELD)
    value = parse_correction(body.field, body.value)
    occ = parse_uuid(occurrence_id)
    o = _authorize(ctx, occ)
    with tenant_tx(system_ctx(ctx.office_id)) as conn:
        old_value = getattr(o, body.field)
        conn.execute(text(f"UPDATE occurrences SET {body.field} = :v, verification_status = 'corrected',"
                          " verified_by = :u, verified_at = now(), review_note = :n WHERE id = :o"),
                     {"v": value, "u": ctx.user_id, "n": body.note, "o": occ})
        entry = {"value": _s(old_value), "at": datetime.now(UTC).isoformat(), "by": str(ctx.user_id)}
        updated = conn.execute(
            text("UPDATE fact_values SET normalized_value = :v, status = 'corrected', corrected_by = :u,"
                 " corrected_at = now(), note = :n, previous = previous || CAST(:p AS jsonb)"
                 " WHERE occurrence_id = :o AND field = :f RETURNING id"),
            {"v": _s(value), "u": ctx.user_id, "n": body.note, "p": json.dumps([entry]), "o": occ, "f": body.field},
        ).first()
        if updated is None:
            conn.execute(
                text("INSERT INTO fact_values (office_id, document_id, occurrence_id, field, original_text,"
                     " normalized_value, source_path, extraction_version, status, corrected_by, corrected_at, note,"
                     " previous) VALUES (app_office(), :d, :o, :f, NULL, :v, CAST(:sp AS jsonb), :ev, 'corrected',"
                     " :u, now(), :n, CAST(:p AS jsonb))"),
                {"d": o.document_id, "o": occ, "f": body.field, "v": _s(value),
                 "sp": json.dumps({"manual": True, "version_id": str(o.version_id)}), "ev": o.extraction_version,
                 "u": ctx.user_id, "n": body.note, "p": json.dumps([entry])},
            )
        reattach(conn, occ)
        _finish(conn, ctx, {o.version_id})
        audit(conn, "record_correct", ctx.user_id, "occurrence", occ, field=body.field)
    with tenant_tx(ctx) as conn:
        return _detail(conn, occ)


def _candidate(ctx: TenantContext, cand_id: str):
    cid = parse_uuid(cand_id)
    with tenant_tx(ctx) as conn:
        c = conn.execute(text("SELECT * FROM dedup_candidates WHERE id = :c AND status = 'open'"), {"c": cid}).first()
        visible = c is not None and all(
            conn.execute(text("SELECT 1 FROM transactions WHERE id = :t"), {"t": t}).first()
            for t in (c.transaction_a, c.transaction_b)
        )
    if not visible or not _fully_visible(ctx, [c.transaction_a, c.transaction_b]):
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
    return c


def _versions_of(conn: Connection, txn_ids: list[UUID]) -> set[UUID]:
    return set(conn.execute(text("SELECT DISTINCT version_id FROM occurrences WHERE transaction_id = ANY(:t)"),
                            {"t": txn_ids}).scalars())


@router.post("/dedup/{candidate_id}/merge")
def merge(candidate_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    c = _candidate(ctx, candidate_id)
    with tenant_tx(system_ctx(ctx.office_id)) as conn:
        lock_office(conn)
        versions = _versions_of(conn, [c.transaction_a, c.transaction_b])
        conn.execute(text("UPDATE dedup_candidates SET status = 'merged', resolved_by = :u, resolved_at = now()"
                          " WHERE id = :c"), {"u": ctx.user_id, "c": c.id})
        conn.execute(text("UPDATE occurrences SET transaction_id = :a WHERE transaction_id = :b"),
                     {"a": c.transaction_a, "b": c.transaction_b})
        conn.execute(text("UPDATE dedup_candidates SET transaction_a = :a WHERE transaction_a = :b AND status = 'open'"),
                     {"a": c.transaction_a, "b": c.transaction_b})
        conn.execute(text("DELETE FROM dedup_candidates WHERE transaction_b = :b OR transaction_a = :b"),
                     {"b": c.transaction_b})
        delete_orphans(conn)
        _finish(conn, ctx, versions)
        audit(conn, "dedup_merge", ctx.user_id, "dedup_candidate", c.id)
    return {"ok": True}


@router.post("/dedup/{candidate_id}/keep-separate")
def keep_separate(candidate_id: str, ctx: TenantContext = Depends(get_ctx)) -> dict:
    c = _candidate(ctx, candidate_id)
    with tenant_tx(system_ctx(ctx.office_id)) as conn:
        versions = _versions_of(conn, [c.transaction_a, c.transaction_b])
        conn.execute(text("UPDATE dedup_candidates SET status = 'kept_separate', resolved_by = :u, resolved_at = now()"
                          " WHERE id = :c"), {"u": ctx.user_id, "c": c.id})
        _finish(conn, ctx, versions)
        audit(conn, "dedup_keep_separate", ctx.user_id, "dedup_candidate", c.id)
    return {"ok": True}
