"""Migration 0015: ``message_diagnostics.limits_hit`` is added and dropped cleanly, and row security stays forced.
Synthetic data only."""

import pytest
from sqlalchemy import text

from alembic import command
from app.db import reset_engine
from tests.conftest import alembic_config

pytestmark = pytest.mark.db


def _columns(db) -> dict[str, str | None]:
    with db.connect() as conn:
        return {r.column_name: r.column_default for r in conn.execute(text(
            "SELECT column_name, column_default FROM information_schema.columns"
            " WHERE table_name = 'message_diagnostics'"))}


def _forced(db) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'message_diagnostics'")
                            ).scalar_one()


def test_the_limits_column_comes_and_goes_with_row_security_forced(db):
    assert "'[]'::jsonb" in (_columns(db).get("limits_hit") or "") and _forced(db)
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0014")
        reset_engine()
        assert "limits_hit" not in _columns(db) and _forced(db)
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert "limits_hit" in _columns(db) and _forced(db)
