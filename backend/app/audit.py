"""Audit log for sensitive actions. Records ids and event types only — never document text or secrets."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, text


def audit(conn: Connection, action: str, user_id: UUID | None, target_type: str | None = None,
          target_id: Any = None, **details: Any) -> None:
    conn.execute(
        text(
            "INSERT INTO audit_events (office_id, user_id, action, target_type, target_id, details)"
            " VALUES (app_office(), :u, :a, :tt, :ti, CAST(:d AS jsonb))"
        ),
        {
            "u": user_id,
            "a": action,
            "tt": target_type,
            "ti": str(target_id) if target_id is not None else None,
            "d": json.dumps(details, default=str, ensure_ascii=False),
        },
    )
