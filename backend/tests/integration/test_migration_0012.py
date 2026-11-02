"""Migration 0012: ``image_readings`` comes and goes cleanly, is office-scoped under forced row security, and is
reachable only under an office's system context (ingestion), never by a user role. Synthetic data only."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_office

pytestmark = pytest.mark.db


def _exists(db) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT to_regclass('image_readings') IS NOT NULL")).scalar_one()


def _forced(db) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT relforcerowsecurity AND relrowsecurity FROM pg_class"
                                 " WHERE relname = 'image_readings'")).scalar_one()


def _insert(conn, digest: str, status: str = "read") -> None:
    conn.execute(text("INSERT INTO image_readings (office_id, content_hash, reader_version, model_config, crop_scale,"
                      " status, reading) VALUES (app_office(), :h, 'regions-v1', 'scripted:low', 'native<=2048', :s,"
                      " CAST('{\"status\": \"read\", \"method\": \"vision\", \"text\": \"סמל\"}' AS jsonb))"),
                 {"h": digest, "s": status})


def test_image_readings_come_and_go_and_stay_with_the_office_system(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    assert _exists(db) and _forced(db)
    with db.connect() as conn:
        columns = {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'image_readings'"))}
    assert "document_id" not in columns and {"office_id", "content_hash", "model_config", "crop_scale"} <= columns
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0011")
        reset_engine()
        assert not _exists(db)
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _exists(db) and _forced(db)

    with tenant_tx(a.system) as conn:
        _insert(conn, "h1")
        assert conn.execute(text("SELECT reading->>'text' FROM image_readings")).scalar_one() == "סמל"
    with tenant_tx(b.system) as conn:  # another office sees nothing and can hold the same content apart
        assert conn.execute(text("SELECT count(*) FROM image_readings")).scalar_one() == 0
        _insert(conn, "h1")
    for role in ("admin", "employee"):  # users never reach the cache, not even their own office's
        with tenant_tx(a.ctx(role)) as conn:
            assert conn.execute(text("SELECT count(*) FROM image_readings")).scalar_one() == 0
        with pytest.raises(DBAPIError), tenant_tx(a.ctx(role)) as conn:
            _insert(conn, "h2")
    with pytest.raises(DBAPIError), tenant_tx(a.system) as conn:  # failures are never stored
        _insert(conn, "h3", status="unread")
    with tenant_tx(a.system) as conn:
        assert conn.execute(text("SELECT count(*) FROM image_readings")).scalar_one() == 1
