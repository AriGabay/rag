"""Request dependencies: resolve the session cookie into a TenantContext.

The office comes only from the server-side session row. Any office id a client sends in a
body or query string is ignored by design (R30)."""

from __future__ import annotations

from uuid import UUID

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import text

from app.db import TenantContext, anonymous_tx, tenant_tx
from app.security import hash_session_token

SESSION_COOKIE = "rag_session"

NOT_AUTHENTICATED = "נדרשת התחברות"
FORBIDDEN = "אין הרשאה לפעולה זו"
NOT_FOUND = "הפריט לא נמצא"


def get_ctx(request: Request) -> TenantContext:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, NOT_AUTHENTICATED)
    with anonymous_tx() as conn:
        row = conn.execute(
            text("SELECT * FROM auth_resolve_session(:h)"), {"h": hash_session_token(token)}
        ).one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, NOT_AUTHENTICATED)
    base = TenantContext(office_id=row.office_id, user_id=row.user_id, role=row.role)
    with tenant_tx(base) as conn:
        user = conn.execute(
            text("SELECT u.can_upload, ARRAY(SELECT ug.group_id FROM user_groups ug WHERE ug.user_id = u.id"
                 " ORDER BY ug.group_id) AS groups FROM users u WHERE u.id = :u"),
            {"u": row.user_id},
        ).one()
    return TenantContext(office_id=row.office_id, user_id=row.user_id, role=row.role,
                         group_ids=tuple(user.groups), can_upload=bool(user.can_upload) or row.role == "admin")


def require_admin(ctx: TenantContext = Depends(get_ctx)) -> TenantContext:
    if ctx.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, FORBIDDEN)
    return ctx


def parse_uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, NOT_FOUND) from exc
