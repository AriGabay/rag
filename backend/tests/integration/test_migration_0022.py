"""Migration 0022: ``version_dates.primary_section`` (round 12 U3b, KTD11; R15, R16).

The column is a boolean that is never null and defaults to false; the revision comes and goes without touching a
version_dates row, a document, a version or a conversation: downgrade drops the column and nothing else. Synthetic data
only."""

import pytest
from sqlalchemy import text

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_office

pytestmark = pytest.mark.db


def _column(db):
    with db.connect() as conn:
        return conn.execute(text(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns"
            " WHERE table_name = 'version_dates' AND column_name = 'primary_section'")).first()


def _date_row(conn, doc, ver, n: int, dates_version: str = "dates-v2") -> None:
    conn.execute(text(
        "INSERT INTO version_dates (office_id, document_id, version_id, dates_version, block_index, iso, precision,"
        " written, status) VALUES (app_office(), :d, :v, :dv, :n, '2025-03-01', 'day', 'x', 'stated')"),
        {"d": doc, "v": ver, "dv": dates_version, "n": n})


def test_the_column_is_a_non_null_boolean_defaulting_to_false(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    col = _column(db)
    assert col is not None and (col.data_type, col.is_nullable) == ("boolean", "NO") and "false" in col.column_default
    with tenant_tx(a.ctx()) as conn:
        _date_row(conn, doc, ver, 1)
        assert conn.execute(text("SELECT primary_section FROM version_dates")).scalar_one() is False


def test_downgrade_drops_only_the_column_and_an_upgrade_again_keeps_every_row(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    with tenant_tx(a.ctx()) as conn:
        _date_row(conn, doc, ver, 1)
        conn.execute(text("UPDATE version_dates SET primary_section = true"))
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0021")
        reset_engine()
        assert _column(db) is None
        with tenant_tx(a.ctx()) as conn:
            assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 1
        with tenant_tx(a.system) as conn:
            assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM document_versions")).scalar_one() == 1
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _column(db) is not None
    with tenant_tx(a.ctx()) as conn:   # the row is kept; its flag reads false again (the dates are derived again)
        assert conn.execute(text("SELECT count(*), bool_or(primary_section) FROM version_dates")).one() == (1, False)
