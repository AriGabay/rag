"""Login, logout, current user (U3)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import text

from app.audit import audit
from app.config import get_settings
from app.db import TenantContext, anonymous_tx, tenant_tx
from app.deps import SESSION_COOKIE, get_ctx
from app.security import hash_session_token, new_session_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])

LOGIN_FAILED = "פרטי ההתחברות שגויים"
LOCKED = "יותר מדי ניסיונות כושלים. נסו שוב בעוד מספר דקות"
MAX_FAILURES = 10
FAILURE_WINDOW_MIN = 15


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login")
def login(body: LoginRequest, response: Response) -> dict:
    with anonymous_tx() as conn:
        row = conn.execute(text("SELECT * FROM auth_login_lookup(:e)"), {"e": body.email.strip()}).one_or_none()
    ok = verify_password(row.password_hash if row else None, body.password)
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, LOGIN_FAILED)

    ctx = TenantContext(office_id=row.office_id, user_id=row.user_id, role=row.role)
    with tenant_tx(ctx) as conn:
        failures = conn.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE action = 'login_failed' AND user_id = :u"
                " AND created_at > now() - make_interval(mins => :m)"
            ),
            {"u": row.user_id, "m": FAILURE_WINDOW_MIN},
        ).scalar_one()
        if failures >= MAX_FAILURES:
            audit(conn, "login_locked", row.user_id, "user", row.user_id)
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, LOCKED)
        if not ok or not row.is_active:
            audit(conn, "login_failed", row.user_id, "user", row.user_id)
    if not ok or not row.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, LOGIN_FAILED)

    settings = get_settings()
    token = new_session_token()
    expires = datetime.now(UTC) + timedelta(hours=settings.session_ttl_hours)
    with tenant_tx(ctx) as conn:
        conn.execute(
            text("INSERT INTO sessions (token_hash, user_id, office_id, expires_at) VALUES (:h, :u, app_office(), :x)"),
            {"h": hash_session_token(token), "u": row.user_id, "x": expires},
        )
        audit(conn, "login", row.user_id, "user", row.user_id)
    response.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax", secure=settings.cookie_secure,
        max_age=settings.session_ttl_hours * 3600, path="/",
    )
    return {"ok": True}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        try:
            ctx = get_ctx(request)
        except HTTPException:
            ctx = None
        if ctx is not None:
            with tenant_tx(ctx) as conn:
                conn.execute(text("DELETE FROM sessions WHERE token_hash = :h"), {"h": hash_session_token(token)})
                audit(conn, "logout", ctx.user_id, "user", ctx.user_id)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(ctx: TenantContext = Depends(get_ctx)) -> dict:
    with tenant_tx(ctx) as conn:
        user = conn.execute(
            text("SELECT id, email, full_name, role, can_upload FROM users WHERE id = :u"), {"u": ctx.user_id}
        ).one()
        office = conn.execute(text("SELECT id, name FROM offices")).one()
        groups = conn.execute(
            text(
                "SELECT g.id, g.name FROM document_groups g"
                + ("" if ctx.is_admin else " JOIN user_groups ug ON ug.group_id = g.id AND ug.user_id = :u")
                + " ORDER BY g.name"
            ),
            {"u": ctx.user_id},
        ).all()
    return {
        "user": {"id": str(user.id), "email": user.email, "full_name": user.full_name, "role": user.role,
                 "can_upload": bool(user.can_upload) or user.role == "admin"},
        "office": {"id": str(office.id), "name": office.name},
        "groups": [{"id": str(g.id), "name": g.name} for g in groups],
        "demo_mode": get_settings().demo_mode,
    }
