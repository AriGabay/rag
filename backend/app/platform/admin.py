"""Office administration: users, groups, cloud-provider setting, coverage (U12, R31, R34)."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import Connection, text
from sqlalchemy.exc import IntegrityError

from app.answering.content import effective_provider
from app.audit import audit
from app.config import get_settings
from app.db import TenantContext, bump_data_version, tenant_tx
from app.deps import NOT_FOUND, parse_uuid, require_admin
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
    row = conn.execute(text("SELECT * FROM office_settings")).one()
    s = get_settings()
    return {"cloud_llm_enabled": row.cloud_llm_enabled, "provider_name": "Anthropic (Claude)", "model": s.anthropic_model,
            "effective_provider": effective_provider(conn),
            "acknowledged_at": row.acknowledged_at.isoformat() if row.acknowledged_at else None}


@router.get("/settings")
def get_office_settings(ctx: TenantContext = Depends(require_admin)) -> dict:
    with tenant_tx(ctx) as conn:
        return _settings_json(conn)


@router.put("/settings")
def put_office_settings(body: SettingsBody, ctx: TenantContext = Depends(require_admin)) -> dict:
    if body.cloud_llm_enabled and not body.acknowledge:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, MSG_ACK)
    with tenant_tx(ctx) as conn:
        conn.execute(
            text("UPDATE office_settings SET cloud_llm_enabled = :e, cloud_provider = :p,"
                 " acknowledged_by = CASE WHEN :e THEN :u ELSE acknowledged_by END,"
                 " acknowledged_at = CASE WHEN :e THEN now() ELSE acknowledged_at END,"
                 " settings_version = settings_version + 1"),
            {"e": body.cloud_llm_enabled, "p": "anthropic" if body.cloud_llm_enabled else None, "u": ctx.user_id},
        )
        audit(conn, "provider_setting", ctx.user_id, "office_settings", ctx.office_id, enabled=body.cloud_llm_enabled)
        return _settings_json(conn)


@router.get("/coverage")
def coverage_summary(ctx: TenantContext = Depends(require_admin)) -> dict:
    with tenant_tx(ctx) as conn:
        by_status = dict(conn.execute(text(
            "SELECT status, count(*) FROM (SELECT DISTINCT ON (v.document_id) v.status FROM document_versions v"
            " JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL"
            " ORDER BY v.document_id, v.version_no DESC) x GROUP BY status")).all())
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
