"""Migration 0009: ``message_diagnostics.resolution`` is added and dropped cleanly, and row security stays forced.
Synthetic data only."""

import pytest
from sqlalchemy import text

from alembic import command
from app.db import reset_engine
from tests.conftest import alembic_config

pytestmark = pytest.mark.db


def _columns(db) -> set[str]:
    with db.connect() as conn:
        return {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'message_diagnostics'"))}


def _forced(db) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT relforcerowsecurity FROM pg_class WHERE relname = 'message_diagnostics'")
                            ).scalar_one()


def test_the_resolution_column_comes_and_goes_with_row_security_forced(db):
    assert "resolution" in _columns(db) and _forced(db)
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0008")
        reset_engine()
        assert "resolution" not in _columns(db) and _forced(db)
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert "resolution" in _columns(db) and _forced(db)
