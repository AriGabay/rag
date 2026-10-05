"""Database access with transaction-local tenant context (KTD4).

Every tenant-scoped unit of work runs inside ``tenant_tx``: one transaction that first sets
``app.office_id``, ``app.user_id`` and ``app.role`` with ``set_config(..., true)``. The settings
die with the transaction, so a pooled connection can never carry one office's context into
another request. RLS policies read these settings and match nothing when they are missing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import lru_cache
from uuid import UUID

from sqlalchemy import Connection, Engine, create_engine, text

from app.config import get_settings

logger = logging.getLogger(__name__)

ROLES = ("admin", "employee", "system")


@dataclass(frozen=True)
class TenantContext:
    office_id: UUID
    user_id: UUID | None
    role: str
    group_ids: tuple[UUID, ...] = field(default_factory=tuple)
    can_upload: bool = False

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"unknown role {self.role!r}")

    @property
    def is_admin(self) -> bool:
        return self.role in ("admin", "system")


def system_ctx(office_id: UUID) -> TenantContext:
    """Context for server-side work in one office (worker jobs, authorized maintenance)."""
    return TenantContext(office_id=office_id, user_id=None, role="system")


@lru_cache
def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url, pool_pre_ping=True, pool_size=10, max_overflow=10)


def reset_engine() -> None:
    """Drop the cached engine (tests switch databases)."""
    if get_engine.cache_info().currsize:
        get_engine().dispose()
    get_engine.cache_clear()


def _set_context(conn: Connection, ctx: TenantContext) -> None:
    conn.execute(
        text(
            "SELECT set_config('app.office_id', :office, true),"
            " set_config('app.user_id', :user, true),"
            " set_config('app.role', :role, true)"
        ),
        {"office": str(ctx.office_id), "user": str(ctx.user_id) if ctx.user_id else "", "role": ctx.role},
    )


@contextmanager
def tenant_tx(ctx: TenantContext, engine: Engine | None = None) -> Iterator[Connection]:
    """Open one transaction scoped to ``ctx``. Commits on success, rolls back on error."""
    with (engine or get_engine()).begin() as conn:
        _set_context(conn, ctx)
        yield conn


@contextmanager
def anonymous_tx(engine: Engine | None = None) -> Iterator[Connection]:
    """Transaction with no tenant context: only the SECURITY DEFINER lookups return rows."""
    with (engine or get_engine()).begin() as conn:
        yield conn


def bump_data_version(conn: Connection) -> int:
    """Increment the office data version (KTD13). Call inside the tenant transaction that made the change."""
    return conn.execute(
        text("UPDATE office_data_versions SET version = version + 1 WHERE office_id = app_office() RETURNING version")
    ).scalar_one()


def current_data_version(conn: Connection) -> int:
    return conn.execute(text("SELECT version FROM office_data_versions WHERE office_id = app_office()")).scalar_one()


def check_database_locale(engine: Engine | None = None) -> None:
    """Fail loudly when the database cannot index Hebrew trigrams (a C locale returns none)."""
    with (engine or get_engine()).connect() as conn:
        trigrams = conn.execute(text("SELECT show_trgm('רמת גן')")).scalar_one()
        ctype = conn.execute(text("SELECT datctype FROM pg_database WHERE datname = current_database()")).scalar_one()
    if not trigrams:
        raise RuntimeError(f"database ctype {ctype!r} produces no Hebrew trigrams; initialize with a UTF-8 locale")
    dim = get_settings().embedding_dim
    with (engine or get_engine()).connect() as conn:
        col = conn.execute(
            text(
                "SELECT format_type(a.atttypid, a.atttypmod) FROM pg_attribute a"
                " WHERE a.attrelid = 'chunks'::regclass AND a.attname = 'embedding'"
            )
        ).scalar_one()
    if col != f"vector({dim})":
        raise RuntimeError(f"chunks.embedding is {col} but EMBEDDING_DIM is {dim}; run the dimension migration")
