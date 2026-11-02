"""Migration 0014: ``region_readings`` comes and goes cleanly, is under forced row security, and a reading is
reachable only through a document the user may see: another office sees nothing, a user outside the document's
group sees nothing (cached or not) and cannot store one for it. Synthetic data only."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db


def _exists(db) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT to_regclass('region_readings') IS NOT NULL")).scalar_one()


def _forced(db) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT relforcerowsecurity AND relrowsecurity FROM pg_class"
                                 " WHERE relname = 'region_readings'")).scalar_one()


def _insert(conn, doc, ver, region: str = "block:3", status: str = "read_uncertain") -> None:
    conn.execute(text(
        "INSERT INTO region_readings (office_id, document_id, version_id, reading_id, region, reader_version,"
        " model_config, page, status, reading) VALUES (app_office(), :d, :v, 'reading-1', :r, 'inspect-v1',"
        " 'scripted:low', 2, :s, CAST('{\"status\": \"read_uncertain\", \"method\": \"vision\","
        " \"text\": \"דמי שכירות 41,300\"}' AS jsonb))"), {"d": doc, "v": ver, "r": region, "s": status})


def _count(conn) -> int:
    return conn.execute(text("SELECT count(*) FROM region_readings")).scalar_one()


def test_region_readings_come_and_go_and_follow_the_document_permissions(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    closed = make_group(a, "קבוצה סגורה")
    inside = make_user(a, "inside@example.test", [a.default_group_id, closed])
    outside = make_user(a, "outside@example.test", [a.default_group_id])
    assert _exists(db) and _forced(db)
    with db.connect() as conn:
        columns = {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'region_readings'"))}
    assert {"office_id", "document_id", "version_id", "reading_id", "region", "reader_version",
            "model_config", "status", "reading"} <= columns
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0013")
        reset_engine()
        assert not _exists(db)
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _exists(db) and _forced(db)

    doc, ver = make_document(a, closed, title="שומה סינתטית סגורה")
    with tenant_tx(a.ctx("employee", inside)) as conn:  # a user who may see the document stores and reads it
        _insert(conn, doc, ver)
        assert conn.execute(text("SELECT reading->>'text' FROM region_readings")).scalar_one() == "דמי שכירות 41,300"
    with tenant_tx(a.ctx("employee", outside)) as conn:  # cached or not: outside the group it does not exist
        assert _count(conn) == 0
    with pytest.raises(DBAPIError), tenant_tx(a.ctx("employee", outside)) as conn:
        _insert(conn, doc, ver, region="page:2")
    with tenant_tx(b.ctx()) as conn:  # another office sees nothing
        assert _count(conn) == 0
    with pytest.raises(DBAPIError), tenant_tx(a.ctx()) as conn:  # failures are never stored
        _insert(conn, doc, ver, region="block:4", status="unread")
    with pytest.raises(DBAPIError), tenant_tx(a.ctx()) as conn:  # one reading per region, reading and reader
        _insert(conn, doc, ver)
    with tenant_tx(a.ctx()) as conn:
        assert _count(conn) == 1
