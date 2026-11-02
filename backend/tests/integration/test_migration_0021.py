"""Migration 0021: ``version_identities`` (round 12 U5, KTD1, KTD15; R5, R21).

The table keeps row security forced, follows the permissions of its document and goes with its version; its fields are
constrained; the revision comes and goes without touching a document, version, unit, date or conversation row: downgrade
drops the table and nothing else. Synthetic data only."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db


def _exists(db, table: str) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}).scalar_one()


def _forced(db, table: str) -> bool:
    with db.connect() as conn:
        return bool(conn.execute(text("SELECT relforcerowsecurity AND relrowsecurity FROM pg_class"
                                      " WHERE relname = :t"), {"t": table}).scalar())


def _fact(conn, doc, ver, n: int = 3, kind: str = "parcel", block: str = "8201", parcel: str = "11",
          section: str = "label") -> None:
    conn.execute(text(
        "INSERT INTO version_identities (office_id, document_id, version_id, identity_version, block_index, kind, block,"
        " parcel, written, section) VALUES (app_office(), :d, :v, 'identity-v1', :n, :k, :b, :p, 'גוש 8201 חלקה 11', :s)"),
        {"d": doc, "v": ver, "n": n, "k": kind, "b": block, "p": parcel, "s": section})


def test_upgrade_creates_the_table_with_forced_row_security_and_downgrade_drops_only_it(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("INSERT INTO conversations (office_id, user_id, title, engine) VALUES"
                          " (app_office(), :u, 'שיחה', 'rag')"), {"u": a.admin_id})
        conn.execute(text(
            "INSERT INTO version_dates (office_id, document_id, version_id, dates_version, block_index, iso, precision,"
            " written, status) VALUES (app_office(), :d, :v, 'dates-v1', 1, '2025-03-01', 'day', 'x', 'stated')"),
            {"d": doc, "v": ver})
        _fact(conn, doc, ver)
    assert _exists(db, "version_identities") and _forced(db, "version_identities")
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0020")
        reset_engine()
        assert not _exists(db, "version_identities")
        assert _exists(db, "version_dates") and _forced(db, "version_dates")   # the other tables are unchanged
        with tenant_tx(a.ctx()) as conn:
            assert conn.execute(text("SELECT count(*) FROM conversations")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 1
        with tenant_tx(a.system) as conn:
            assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM document_versions")).scalar_one() == 1
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _exists(db, "version_identities") and _forced(db, "version_identities")
    with tenant_tx(a.ctx()) as conn:   # an upgrade again: the identities come again on first use
        assert conn.execute(text("SELECT count(*) FROM version_identities")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 1


def test_version_identities_follow_the_permissions_of_their_document_and_go_with_their_version(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    closed = make_group(a, "קבוצה סגורה")
    inside = make_user(a, "inside@example.test", [a.default_group_id, closed])
    outside = make_user(a, "outside@example.test", [a.default_group_id])
    doc, ver = make_document(a, closed, title="שומה סינתטית סגורה")
    with tenant_tx(a.ctx("employee", inside)) as conn:
        _fact(conn, doc, ver, 0)
        _fact(conn, doc, ver, 3, kind="city", section="title")
        assert conn.execute(text("SELECT count(*) FROM version_identities")).scalar_one() == 2
        with pytest.raises(DBAPIError), conn.begin_nested():   # the derivation's fields are constrained
            conn.execute(text("UPDATE version_identities SET kind = 'guess' WHERE block_index = 0"))
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.execute(text("UPDATE version_identities SET section = 'anywhere' WHERE block_index = 0"))
    with tenant_tx(a.ctx("employee", outside)) as conn:   # outside the group they do not exist
        assert conn.execute(text("SELECT count(*) FROM version_identities")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(a.ctx("employee", outside)) as conn:
        _fact(conn, doc, ver, 5)
    with tenant_tx(b.ctx()) as conn:   # another office sees nothing, even as its admin, and cannot write against it
        assert conn.execute(text("SELECT count(*) FROM version_identities")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(b.ctx()) as conn:
        _fact(conn, doc, ver, 6)
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM version_identities")).scalar_one() == 2
        conn.execute(text("DELETE FROM document_versions WHERE id = :v"), {"v": ver})
        assert conn.execute(text("SELECT count(*) FROM version_identities")).scalar_one() == 0


def test_version_identities_are_indexed_by_version_and_derivation(db):
    with db.connect() as conn:
        defs = [r[0] for r in conn.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename = 'version_identities'"))]
    assert any("(version_id, identity_version)" in d for d in defs), defs
    assert any("(office_id, document_id)" in d for d in defs), defs
