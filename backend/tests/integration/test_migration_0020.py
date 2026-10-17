"""Migration 0020: ``conversations.prefer_recent``, ``messages.time_preference`` and ``version_dates`` (round 11 U5, KTD1,
KTD3, KTD15; R2, R14).

A conversation that exists before the revision reads the switch off and a message reads no recorded state (NULL); a
conversation created after it starts on; ``version_dates`` keeps row security forced, follows the permissions of its
document and goes with its version; the revision comes and goes without touching a conversation, message or document
row. Synthetic data only."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db


def _columns(db, table: str) -> set[str]:
    with db.connect() as conn:
        return {r[0] for r in conn.execute(text("SELECT column_name FROM information_schema.columns"
                                                " WHERE table_name = :t"), {"t": table})}


def _exists(db, table: str) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}).scalar_one()


def _forced(db, table: str) -> bool:
    with db.connect() as conn:
        return bool(conn.execute(text("SELECT relforcerowsecurity AND relrowsecurity FROM pg_class"
                                      " WHERE relname = :t"), {"t": table}).scalar())


def _date(conn, doc, ver, n: int = 0, iso: str = "2025-03-01", status: str = "stated") -> None:
    conn.execute(text(
        "INSERT INTO version_dates (office_id, document_id, version_id, dates_version, block_index, iso, precision,"
        " written, status) VALUES (app_office(), :d, :v, 'dates-v1', :n, :i, 'day', 'המועד הקובע 01/03/2025', :s)"),
        {"d": doc, "v": ver, "n": n, "i": iso, "s": status})


def test_existing_conversations_read_off_new_ones_start_on_and_nothing_is_lost(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0019")
        reset_engine()
        assert "prefer_recent" not in _columns(db, "conversations")
        assert "time_preference" not in _columns(db, "messages") and not _exists(db, "version_dates")
        with tenant_tx(a.ctx()) as conn:  # a conversation and a message from before the round
            old = conn.execute(text("INSERT INTO conversations (office_id, user_id, title, engine) VALUES"
                                    " (app_office(), :u, 'שיחה ישנה', 'rag') RETURNING id"), {"u": a.admin_id}).scalar_one()
            conn.execute(text("INSERT INTO messages (office_id, conversation_id, user_id, role, content, status)"
                              " VALUES (app_office(), :c, :u, 'user', 'שאלה ישנה', 'done')"), {"c": old, "u": a.admin_id})
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert "prefer_recent" in _columns(db, "conversations") and "time_preference" in _columns(db, "messages")
    assert _exists(db, "version_dates") and _forced(db, "version_dates")
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT prefer_recent FROM conversations WHERE id = :c"), {"c": old}).scalar_one() is False
        assert conn.execute(text("SELECT time_preference FROM messages WHERE conversation_id = :c"),
                            {"c": old}).scalar_one() is None  # a message from before the round carries no state
        new = conn.execute(text("INSERT INTO conversations (office_id, user_id, title, engine) VALUES"
                                " (app_office(), :u, 'שיחה חדשה', 'rag') RETURNING prefer_recent"),
                           {"u": a.admin_id}).scalar_one()
        assert new is True
        assert conn.execute(text("SELECT count(*) FROM conversations")).scalar_one() == 2
        assert conn.execute(text("SELECT count(*) FROM messages")).scalar_one() == 1


def test_the_column_is_not_null_and_the_revision_downgrades_cleanly(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    with tenant_tx(a.ctx()) as conn:
        cid = conn.execute(text("INSERT INTO conversations (office_id, user_id, title, engine) VALUES"
                                " (app_office(), :u, 'שיחה', 'rag') RETURNING id"), {"u": a.admin_id}).scalar_one()
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.execute(text("UPDATE conversations SET prefer_recent = NULL WHERE id = :c"), {"c": cid})
        _date(conn, doc, ver)
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0019")
        reset_engine()
        assert not _exists(db, "version_dates")
        assert "prefer_recent" not in _columns(db, "conversations") and "time_preference" not in _columns(db, "messages")
        with tenant_tx(a.ctx()) as conn:  # the conversation and the document are intact
            assert conn.execute(text("SELECT count(*) FROM conversations")).scalar_one() == 1
        with tenant_tx(a.system) as conn:
            assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM document_versions")).scalar_one() == 1
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _exists(db, "version_dates") and _forced(db, "version_dates")
    with tenant_tx(a.ctx()) as conn:  # an upgrade again: the conversation reads off, the dates come again on first use
        assert conn.execute(text("SELECT prefer_recent FROM conversations WHERE id = :c"), {"c": cid}).scalar_one() is False
        assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 0


def test_version_dates_follow_the_permissions_of_their_document_and_go_with_their_version(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    closed = make_group(a, "קבוצה סגורה")
    inside = make_user(a, "inside@example.test", [a.default_group_id, closed])
    outside = make_user(a, "outside@example.test", [a.default_group_id])
    doc, ver = make_document(a, closed, title="שומה סינתטית סגורה")
    with tenant_tx(a.ctx("employee", inside)) as conn:
        _date(conn, doc, ver, 0)
        _date(conn, doc, ver, 3, iso="2024-12-31", status="conflict")
        assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 2
        with pytest.raises(DBAPIError), conn.begin_nested():  # the derivation's fields are constrained
            conn.execute(text("UPDATE version_dates SET precision = 'week' WHERE block_index = 0"))
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.execute(text("UPDATE version_dates SET status = 'guess' WHERE block_index = 0"))
    with tenant_tx(a.ctx("employee", outside)) as conn:  # outside the group they do not exist
        assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(a.ctx("employee", outside)) as conn:
        _date(conn, doc, ver, 5)
    with tenant_tx(b.ctx()) as conn:  # another office sees nothing, even as its admin, and cannot write against it
        assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(b.ctx()) as conn:
        _date(conn, doc, ver, 6)
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 2
        conn.execute(text("DELETE FROM document_versions WHERE id = :v"), {"v": ver})
        assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 0


def test_version_dates_are_indexed_by_version_and_derivation(db):
    with db.connect() as conn:
        defs = [r[0] for r in conn.execute(text("SELECT indexdef FROM pg_indexes WHERE tablename = 'version_dates'"))]
    assert any("(version_id, dates_version)" in d for d in defs), defs
