"""Migration 0024: ``answer_reports`` (round 25 U5, KTD9, R11). Synthetic data only.

The table keeps row security forced with the owner-or-admin shape of ``message_diagnostics``, links to the message and the
conversation with ``ON DELETE SET NULL`` (a retry or a deleted conversation leaves the report), takes one report per user
and message, and goes on a downgrade without touching any other row."""

import json

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_office, make_user

pytestmark = pytest.mark.db


def _exists(db) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT to_regclass('answer_reports') IS NOT NULL")).scalar_one()


def _forced(db) -> bool:
    with db.connect() as conn:
        return bool(conn.execute(text("SELECT relforcerowsecurity AND relrowsecurity FROM pg_class"
                                      " WHERE relname = 'answer_reports'")).scalar())


def _conversation(conn, user_id) -> str:
    return str(conn.execute(text("INSERT INTO conversations (office_id, user_id, title, engine) VALUES"
                                 " (app_office(), :u, 'שיחה', 'rag') RETURNING id"), {"u": user_id}).scalar_one())


def _assistant(conn, cid, user_id, status: str = "done") -> str:
    return str(conn.execute(text(
        "INSERT INTO messages (office_id, conversation_id, user_id, role, status, content) VALUES"
        " (app_office(), :c, :u, 'assistant', :s, 'תשובה') RETURNING id"), {"c": cid, "u": user_id, "s": status}).scalar_one())


def _report(conn, cid, mid, user_id, category: str = "wrong", note: str = "", snapshot: dict | None = None) -> str:
    return str(conn.execute(text(
        "INSERT INTO answer_reports (office_id, user_id, conversation_id, message_id, category, note, message_status, snapshot)"
        " VALUES (app_office(), :u, :c, :m, :cat, :n, 'done', CAST(:s AS jsonb)) RETURNING id"),
        {"u": user_id, "c": cid, "m": mid, "cat": category, "n": note, "s": json.dumps(snapshot or {})}).scalar_one())


def test_the_table_exists_with_forced_row_security_and_its_links_set_null(db):
    assert _exists(db) and _forced(db)
    with db.connect() as conn:
        actions = {r.confrelid_name: r.confdeltype for r in conn.execute(text(
            "SELECT c.confrelid::regclass::text AS confrelid_name, c.confdeltype FROM pg_constraint c"
            " WHERE c.conrelid = 'answer_reports'::regclass AND c.contype = 'f'"))}
        indexes = {r.indexname for r in conn.execute(text("SELECT indexname FROM pg_indexes WHERE tablename = 'answer_reports'"))}
        granted = {r.privilege_type for r in conn.execute(text(
            "SELECT privilege_type FROM information_schema.role_table_grants WHERE table_name = 'answer_reports'"
            " AND grantee = 'rag_app'"))}
    assert actions["messages"] == "n" and actions["conversations"] == "n"   # SET NULL
    assert actions["offices"] == "a" and actions["users"] == "a"
    assert {"answer_reports_office_created_idx", "answer_reports_user_message_idx"} <= indexes
    assert granted == {"SELECT", "INSERT", "UPDATE"}   # the application never deletes a report


def test_a_message_or_conversation_that_goes_leaves_the_report_with_empty_links(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    u = make_user(a, "emp@example.test", [a.default_group_id])
    with tenant_tx(a.ctx("employee", u)) as conn:
        c1, c2 = _conversation(conn, u), _conversation(conn, u)
        m1, m2 = _assistant(conn, c1, u), _assistant(conn, c2, u)
        r1 = _report(conn, c1, m1, u, snapshot={"counts": {"rounds": 2}})
        r2 = _report(conn, c2, m2, u)
        conn.execute(text("DELETE FROM messages WHERE id = :m"), {"m": m1})   # what a retry does
        conn.execute(text("DELETE FROM conversations WHERE id = :c"), {"c": c2})   # the conversation, and its messages, go
        rows = {str(r.id): r for r in conn.execute(text("SELECT * FROM answer_reports"))}
    assert rows[r1].message_id is None and str(rows[r1].conversation_id) == c1 and rows[r1].snapshot == {"counts": {"rounds": 2}}
    assert rows[r2].message_id is None and rows[r2].conversation_id is None


def test_one_report_per_user_and_message_and_the_checks_hold(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    u1 = make_user(a, "emp1@example.test", [a.default_group_id])
    u2 = make_user(a, "emp2@example.test", [a.default_group_id])
    with tenant_tx(a.system) as conn:
        c = _conversation(conn, u1)
        m = _assistant(conn, c, u1)
        _report(conn, c, m, u1)
        _report(conn, c, m, u2)   # another user of the same message is another row
        with pytest.raises(DBAPIError), conn.begin_nested():
            _report(conn, c, m, u1)
        with pytest.raises(DBAPIError), conn.begin_nested():
            _report(conn, c, None, u1, category="no_such_category")
        with pytest.raises(DBAPIError), conn.begin_nested():
            _report(conn, c, None, u1, note="x" * 1001)
        _report(conn, c, None, u1)
        _report(conn, c, None, u1)   # rows whose message is gone are not constrained
        assert conn.execute(text("SELECT count(*) FROM answer_reports")).scalar_one() == 4


def test_row_security_owner_writes_admin_reads_other_user_and_office_see_nothing(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    u1 = make_user(a, "emp1@example.test", [a.default_group_id])
    u2 = make_user(a, "emp2@example.test", [a.default_group_id])
    with tenant_tx(a.ctx("employee", u1)) as conn:
        c = _conversation(conn, u1)
        m = _assistant(conn, c, u1)
        _report(conn, c, m, u1)
        with pytest.raises(DBAPIError), conn.begin_nested():   # a report is written as its reporter
            _report(conn, c, None, u2)
    def count(ctx) -> int:
        with tenant_tx(ctx) as conn:
            return conn.execute(text("SELECT count(*) FROM answer_reports")).scalar_one()
    assert count(a.ctx("employee", u1)) == 1 and count(a.ctx()) == 1   # the owner and the office admin
    assert count(a.ctx("employee", u2)) == 0   # another employee of the office
    assert count(b.ctx()) == 0   # another office, even its admin
    with pytest.raises(DBAPIError), tenant_tx(a.ctx()) as conn:   # an admin reads but does not write another user's row
        conn.execute(text("UPDATE answer_reports SET note = 'x'"))
    with tenant_tx(a.ctx("employee", u1)) as conn:   # no application delete
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.execute(text("DELETE FROM answer_reports"))


def test_the_downgrade_drops_only_the_table_and_an_upgrade_on_0023_keeps_every_existing_row(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    u = make_user(a, "emp@example.test", [a.default_group_id])
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0023")
        reset_engine()
        assert not _exists(db)
        with tenant_tx(a.ctx("employee", u)) as conn:   # a seeded 0023 database
            c = _conversation(conn, u)
            m = _assistant(conn, c, u)
            conn.execute(text("INSERT INTO message_diagnostics (message_id, office_id, user_id, document_ids) VALUES"
                              " (:m, app_office(), :u, '{}')"), {"m": m, "u": u})
        command.upgrade(cfg, "head")
        reset_engine()
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _exists(db) and _forced(db)
    with tenant_tx(a.ctx("employee", u)) as conn:
        assert conn.execute(text("SELECT count(*) FROM messages")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM conversations")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM message_diagnostics")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM answer_reports")).scalar_one() == 0
        c = str(conn.execute(text("SELECT id FROM conversations")).scalar_one())
        m = str(conn.execute(text("SELECT id FROM messages")).scalar_one())
        _report(conn, c, m, u)   # and it works
