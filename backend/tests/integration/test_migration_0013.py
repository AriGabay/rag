"""Migration 0013: ``measurements.anchor_lost`` comes and goes cleanly, measurements written before it read as
anchored, and row security on ``measurements`` stays forced. Synthetic data only."""

import pytest
from sqlalchemy import text

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_office

pytestmark = pytest.mark.db


def _has_column(db) -> bool:
    with db.connect() as conn:
        return conn.execute(text(
            "SELECT count(*) FROM information_schema.columns WHERE table_name = 'measurements'"
            " AND column_name = 'anchor_lost'")).scalar_one() == 1


def _forced(db) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT relforcerowsecurity AND relrowsecurity FROM pg_class"
                                 " WHERE relname = 'measurements'")).scalar_one()


def _measurement(conn, doc, ver, key: str) -> None:
    conn.execute(text(
        "INSERT INTO measurements (office_id, document_id, version_id, block_index, statement_key, metric, metric_kind,"
        " value, value_text, quote, extraction_version, status) VALUES (app_office(), :d, :v, 3, :k, 'שווי', 'value',"
        " 1250000, '1,250,000 ₪', 'שווי הנכס 1,250,000 ₪', 'm1', 'verified')"), {"d": doc, "v": ver, "k": key})


def test_anchor_lost_comes_and_goes_and_older_rows_read_as_anchored(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    assert _has_column(db) and _forced(db)
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0012")
        reset_engine()
        assert not _has_column(db) and _forced(db)
        doc, ver = make_document(a, a.default_group_id, title="דוח סינתטי")
        with tenant_tx(a.system) as conn:  # a measurement written before the revision
            _measurement(conn, doc, ver, "b3:old")
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _has_column(db) and _forced(db)
    with tenant_tx(a.system) as conn:
        assert conn.execute(text("SELECT anchor_lost FROM measurements")).scalar_one() is None
        conn.execute(text("UPDATE measurements SET anchor_lost = CAST(:j AS jsonb), block_index = NULL"),
                     {"j": '{"reading_id": "r1", "block_index": 3}'})
        assert conn.execute(text("SELECT anchor_lost->>'reading_id' FROM measurements")).scalar_one() == "r1"
    with tenant_tx(b.system) as conn:  # the tenant policy still isolates the table
        assert conn.execute(text("SELECT count(*) FROM measurements")).scalar_one() == 0
