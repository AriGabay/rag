"""Office administration: users, groups, cloud-provider setting and status, coverage (U12, U2, R8, R31, R34)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app.answering.content import log_usage
from app.audit import audit
from app.db import TenantContext, bump_data_version, tenant_tx
from app.deps import NOT_FOUND, parse_uuid, require_admin
from app.platform.documents import latest_status_counts
from app.providers.status import (
    RETENTION_NOTES,
    office_provider_state,
    purpose_models,
    record_test,
    run_connection_test,
    selected_provider_and_model,
)
from app.security import hash_password

router = APIRouter(prefix="/api/admin", tags=["admin"])

MSG_ACK = "יש לאשר במפורש שקטעי מסמכים רלוונטיים יישלחו לספק הענן"
MSG_EMAIL = "כתובת הדוא״ל כבר קיימת במערכת"
MSG_GROUP = "קבוצה לא קיימת"
MSG_SELF = "לא ניתן להסיר הרשאת מנהל מעצמך"


class NewUser(BaseModel):
    email: str = Field(min_length=3, max_length=200)
    full_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=8, max_length=200)
    role: str = Field(pattern="^(admin|employee)$")
    can_upload: bool = False
    group_ids: list[str] = []


class UserPatch(BaseModel):
    role: str | None = Field(default=None, pattern="^(admin|employee)$")
    can_upload: bool | None = None
    is_active: bool | None = None
    group_ids: list[str] | None = None
    password: str | None = Field(default=None, min_length=8, max_length=200)


class NewGroup(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class SettingsBody(BaseModel):
    cloud_llm_enabled: bool
    acknowledge: bool = False


def _set_groups(conn: Connection, user_id: UUID, group_ids: list[str]) -> None:
    ids = [parse_uuid(g) for g in group_ids]
    found = conn.execute(text("SELECT count(*) FROM document_groups WHERE id = ANY(:ids)"), {"ids": ids}).scalar_one()
    if found != len(set(ids)):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_GROUP)
    conn.execute(text("DELETE FROM user_groups WHERE user_id = :u"), {"u": user_id})
    for g in set(ids):
        conn.execute(text("INSERT INTO user_groups (user_id, group_id, office_id) VALUES (:u, :g, app_office())"),
                     {"u": user_id, "g": g})


@router.get("/users")
def users(ctx: TenantContext = Depends(require_admin)) -> dict:
    with tenant_tx(ctx) as conn:
        rows = conn.execute(text(
            "SELECT u.id, u.email, u.full_name, u.role, u.can_upload, u.is_active,"
            " COALESCE(array_agg(ug.group_id) FILTER (WHERE ug.group_id IS NOT NULL), '{}') AS group_ids"
            " FROM users u LEFT JOIN user_groups ug ON ug.user_id = u.id GROUP BY u.id ORDER BY u.full_name")).all()
    return {"users": [{"id": str(r.id), "email": r.email, "full_name": r.full_name, "role": r.role,
                       "can_upload": r.can_upload, "is_active": r.is_active,
                       "group_ids": [str(g) for g in r.group_ids]} for r in rows]}


@router.post("/users")
def create_user(body: NewUser, ctx: TenantContext = Depends(require_admin)) -> dict:
    try:
        with tenant_tx(ctx) as conn:
            uid = conn.execute(
                text("INSERT INTO users (office_id, email, full_name, password_hash, role, can_upload)"
                     " VALUES (app_office(), :e, :n, :h, :r, :c) RETURNING id"),
                {"e": body.email.strip(), "n": body.full_name.strip(), "h": hash_password(body.password),
                 "r": body.role, "c": body.can_upload},
            ).scalar_one()
            _set_groups(conn, uid, body.group_ids)
            bump_data_version(conn)
            audit(conn, "user_create", ctx.user_id, "user", uid, role=body.role)
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, MSG_EMAIL) from exc
    return {"id": str(uid)}


@router.patch("/users/{user_id}")
def patch_user(user_id: str, body: UserPatch, ctx: TenantContext = Depends(require_admin)) -> dict:
    uid = parse_uuid(user_id)
    with tenant_tx(ctx) as conn:
        if conn.execute(text("SELECT 1 FROM users WHERE id = :u"), {"u": uid}).first() is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND)
        if uid == ctx.user_id and (body.role == "employee" or body.is_active is False):
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_SELF)
        for column in ("role", "can_upload", "is_active"):
            value = getattr(body, column)
            if value is not None:
                conn.execute(text(f"UPDATE users SET {column} = :v WHERE id = :u"), {"v": value, "u": uid})
        if body.password:
            conn.execute(text("UPDATE users SET password_hash = :h WHERE id = :u"),
                         {"h": hash_password(body.password), "u": uid})
        if body.group_ids is not None:
            _set_groups(conn, uid, body.group_ids)
        if body.is_active is False or body.password:
            conn.execute(text("DELETE FROM sessions WHERE user_id = :u"), {"u": uid})
        bump_data_version(conn)
        audit(conn, "user_update", ctx.user_id, "user", uid,
              fields=[k for k, v in body.model_dump().items() if v is not None and k != "password"],
              password_reset=bool(body.password))
    return {"ok": True}


@router.get("/groups")
def groups(ctx: TenantContext = Depends(require_admin)) -> dict:
    with tenant_tx(ctx) as conn:
        rows = conn.execute(text(
            "SELECT g.id, g.name, count(d.id) FILTER (WHERE d.deleted_at IS NULL) AS n FROM document_groups g"
            " LEFT JOIN documents d ON d.group_id = g.id GROUP BY g.id ORDER BY g.name")).all()
    return {"groups": [{"id": str(r.id), "name": r.name, "document_count": r.n} for r in rows]}


@router.post("/groups")
def create_group(body: NewGroup, ctx: TenantContext = Depends(require_admin)) -> dict:
    try:
        with tenant_tx(ctx) as conn:
            gid = conn.execute(text("INSERT INTO document_groups (office_id, name) VALUES (app_office(), :n)"
                                    " RETURNING id"), {"n": body.name.strip()}).scalar_one()
            audit(conn, "group_create", ctx.user_id, "group", gid)
    except IntegrityError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, "קבוצה בשם זה כבר קיימת") from exc
    return {"id": str(gid)}


def _settings_json(conn: Connection) -> dict:
    """Provider, model, key presence (never the key or any part of it), last test and derived mode (KTD5)."""
    row = conn.execute(text("SELECT cloud_llm_enabled, acknowledged_at FROM office_settings")).one()
    state = office_provider_state(conn)
    last = state.last_test
    return {
        "cloud_llm_enabled": row.cloud_llm_enabled, "provider": state.provider, "provider_name": state.provider_name,
        "model": state.model, "purposes": purpose_models(), "key_present": state.key_present,
        "mode": state.mode.value,
        "mode_status": state.status, "untested": state.untested,
        "last_test": {"provider": last.provider, "model": last.model, "ok": last.ok, "status": last.status,
                      "tested_at": last.tested_at.isoformat() if last.tested_at else None} if last else None,
        "retention_note": RETENTION_NOTES.get(state.provider),
        "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None,
    }


@router.get("/settings")
def get_office_settings(ctx: TenantContext = Depends(require_admin)) -> dict:
    with tenant_tx(ctx) as conn:
        return _settings_json(conn)


@router.put("/settings")
def put_office_settings(body: SettingsBody, ctx: TenantContext = Depends(require_admin)) -> dict:
    if body.cloud_llm_enabled and not body.acknowledge:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_ACK)
    provider, _ = selected_provider_and_model()
    with tenant_tx(ctx) as conn:
        conn.execute(
            text("UPDATE office_settings SET cloud_llm_enabled = :e, cloud_provider = :p,"
                 " acknowledged_by = CASE WHEN :e THEN :u ELSE acknowledged_by END,"
                 " acknowledged_at = CASE WHEN :e THEN now() ELSE acknowledged_at END,"
                 " settings_version = settings_version + 1"),
            {"e": body.cloud_llm_enabled, "p": provider if body.cloud_llm_enabled else None, "u": ctx.user_id},
        )
        audit(conn, "provider_setting", ctx.user_id, "office_settings", ctx.office_id, enabled=body.cloud_llm_enabled,
              provider=provider)
        return _settings_json(conn)


@router.post("/provider/test")
def test_provider(ctx: TenantContext = Depends(require_admin)) -> dict:
    """Connection test (KTD5): a synthetic prompt with no office content, so it runs even while cloud use is off.
    The call runs outside any transaction; a result that changes the mode bumps ``settings_version`` so no
    answer cached under the previous mode is served."""
    with tenant_tx(ctx) as conn:
        before = office_provider_state(conn).mode
    outcome = run_connection_test()
    with tenant_tx(ctx) as conn:
        record_test(conn, outcome)
        if outcome.client is not None:
            log_usage(conn, outcome.client, "test", outcome.result, outcome.ok)
        if office_provider_state(conn).mode != before:
            conn.execute(text("UPDATE office_settings SET settings_version = settings_version + 1"))
        audit(conn, "provider_test", ctx.user_id, "office_settings", ctx.office_id, provider=outcome.provider,
              model=outcome.model, status=outcome.status)
        return _settings_json(conn)


@router.get("/coverage")
def coverage_summary(ctx: TenantContext = Depends(require_admin)) -> dict:
    with tenant_tx(ctx) as conn:
        by_status = latest_status_counts(conn)
        rec = conn.execute(text(
            "SELECT count(*) AS total,"
            " count(*) FILTER (WHERE o.verification_status IN ('human_verified', 'corrected')) AS verified,"
            " count(*) FILTER (WHERE o.verification_status = 'auto_extracted') AS awaiting,"
            " count(*) FILTER (WHERE o.verification_status = 'needs_review') AS review"
            " FROM occurrences o JOIN document_versions v ON v.id = o.version_id AND v.is_current"
            " JOIN documents d ON d.id = o.document_id AND d.deleted_at IS NULL")).one()
        open_dedup = conn.execute(text("SELECT count(*) FROM dedup_candidates WHERE status = 'open'")).scalar_one()
    return {"documents_by_status": by_status,
            "records": {"total": rec.total, "verified": rec.verified, "awaiting_verification": rec.awaiting,
                        "needs_review": rec.review},
            "open_dedup_candidates": open_dedup, "review_queue_count": rec.awaiting + rec.review + open_dedup}


# --- reprocessing ------------------------------------------------------------------------------------------

class ReprocessBody(BaseModel):
    all: bool = False  # False: only versions read by an older reader / not yet measured
    # reprocess only: read again the versions whose last reading was held back as worse than the current one
    # (``ingestion.reprocess_regression``), accepting that regression (KTD7)
    accept_regression: bool = False


@router.post("/reprocess")
def reprocess(body: ReprocessBody, ctx: TenantContext = Depends(require_admin)) -> dict:
    """Queue a fresh reading of the office's current documents (blocks, pictures, chunks, embeddings; records and
    reviewed decisions kept). Without ``all``, only versions read by an older reader; a version whose new reading
    was held back as worse than its current one waits for an admin: ``accept_regression`` reads exactly those
    again and lets the new reading replace the current one despite the recorded regression."""
    from app.platform.jobs import enqueue_reindex
    from app.platform.pipeline import INGESTION_VERSION, PDF_INGESTION_VERSION, ingestion_version

    with tenant_tx(ctx) as conn:
        rows = conn.execute(text(
            "SELECT v.id, v.mime_type, v.ingestion->>'ingestion_version' AS iv,"
            " (v.ingestion ? 'reprocess_regression') AS held FROM document_versions v JOIN"
            " documents d ON d.id = v.document_id AND d.deleted_at IS NULL WHERE v.is_current AND v.status IN"
            " ('ready', 'needs_review')")).all()
        if body.accept_regression:
            wanted = [r for r in rows if r.held]
        else:
            wanted = [r for r in rows if body.all or (r.iv != ingestion_version(r.mime_type) and not r.held)]
        queued = [str(r.id) for r in wanted
                  if enqueue_reindex(conn, r.id, ingestion_version(r.mime_type), body.accept_regression)]
        audit(conn, "reprocess", ctx.user_id, "office", ctx.office_id, accept_regression=body.accept_regression)
    return {"queued": len(queued), "versions": queued, "ingestion_version": INGESTION_VERSION,
            "ingestion_versions": {"docx": INGESTION_VERSION, "pdf": PDF_INGESTION_VERSION},
            "held": sum(1 for r in rows if r.held)}


@router.post("/measurements")
def extract_measurements(body: ReprocessBody, ctx: TenantContext = Depends(require_admin)) -> dict:
    """Queue measurement extraction for current versions without a completed run of the current extraction
    version (or all of them). Requires the office's cloud mode; the jobs are skipped otherwise."""
    from app.measurements.extract import EXTRACTION_VERSION
    from app.platform.jobs import enqueue_measurements

    with tenant_tx(ctx) as conn:
        rows = conn.execute(text(
            "SELECT v.id, r.state FROM document_versions v JOIN documents d ON d.id = v.document_id AND d.deleted_at"
            " IS NULL LEFT JOIN measurement_runs r ON r.version_id = v.id AND r.extraction_version = :e"
            " WHERE v.is_current AND v.status IN ('ready', 'needs_review')"), {"e": EXTRACTION_VERSION}).all()
        queued = [str(r.id) for r in rows if (body.all or r.state != "done")
                  and enqueue_measurements(conn, r.id, EXTRACTION_VERSION)]
    return {"queued": len(queued), "extraction_version": EXTRACTION_VERSION}


@router.post("/positions")
def backfill_positions(ctx: TenantContext = Depends(require_admin)) -> dict:
    """Queue the geometry-only backfill (KTD3) for the office's current PDF versions read before positions existed:
    their pages, words and table cells gain positions from the stored file, their reading (and every citation of it)
    stays. It runs after ingestion; versions already queued are left alone."""
    from app.extraction.geometry import POSITIONS_VERSION
    from app.platform.jobs import enqueue_missing_positions

    with tenant_tx(ctx) as conn:
        queued = [str(v) for v in enqueue_missing_positions(conn)]
        audit(conn, "positions_backfill", ctx.user_id, "office", ctx.office_id, queued=len(queued))
    return {"queued": len(queued), "versions": queued, "positions_version": POSITIONS_VERSION}


def _positions_progress(conn: Connection) -> dict:
    """How far the geometry backfill is (KTD3): the office's current PDF versions, how many store positions, and over
    the backfilled ones the blocks and tables that aligned, did not align, or sit on pages that cannot be converted."""
    from app.extraction.geometry import POSITIONS_VERSION

    def total(kind: str, state: str) -> str:
        return (f"COALESCE(sum((v.ingestion->'positions_backfill'->'{kind}'->>'{state}')::int), 0)"
                f" AS {kind}_{state}")

    states = ("aligned", "unaligned", "no_positions")
    r = conn.execute(text(
        "SELECT count(*) AS total, count(*) FILTER (WHERE v.ingestion->>'positions' = :pv) AS positioned,"
        " count(*) FILTER (WHERE v.ingestion ? 'positions_backfill') AS backfilled, "
        + ", ".join(total(k, s) for k in ("blocks", "tables") for s in states)
        + " FROM document_versions v JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL"
        " WHERE v.is_current AND v.status IN ('ready', 'needs_review') AND v.mime_type = 'application/pdf'"),
        {"pv": POSITIONS_VERSION}).one()
    return {"pdf_versions": r.total, "with_positions": r.positioned, "without_positions": r.total - r.positioned,
            "backfilled": r.backfilled,
            "blocks": {s: getattr(r, f"blocks_{s}") for s in states},
            "tables": {s: getattr(r, f"tables_{s}") for s in states}}


@router.get("/jobs")
def jobs_summary(ctx: TenantContext = Depends(require_admin)) -> dict:
    from app.platform.pipeline import regression_summary

    with tenant_tx(ctx) as conn:
        positions = _positions_progress(conn)
        rows = conn.execute(text(
            "SELECT kind, COALESCE(payload->>'mode', '') AS mode, status, count(*) AS n FROM jobs"
            " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3")).all()
        held = conn.execute(text(
            "SELECT v.id, d.title, v.ingestion->'reprocess_regression' AS r FROM document_versions v JOIN documents d"
            " ON d.id = v.document_id AND d.deleted_at IS NULL WHERE v.is_current AND v.ingestion ?"
            " 'reprocess_regression' ORDER BY d.title")).all()
    # new readings held back as worse than the current one: kept until an admin accepts them (KTD7)
    return {"jobs": [{"kind": r.kind + (f":{r.mode}" if r.mode else ""), "status": r.status, "count": r.n}
                     for r in rows],
            "regressions": [{"version_id": str(r.id), "title": r.title, "summary": regression_summary(r.r),
                             "regression": r.r} for r in held],
            "positions": positions}
