"""Migration 0004 (general question engine): constraints, worker dispatch by job kind, and a
downgrade/upgrade round trip on a populated test database (U3, KTD6)."""

import importlib.util
import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app import worker
from app.config import get_settings
from app.db import TenantContext, reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_office, make_user
from tests.integration.test_rls import make_attribute, make_fact, make_question

pytestmark = pytest.mark.db


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, "מסמך א", sha="a" * 64)
    return a, doc, ver


def _enqueue_extract(office, ver, attr) -> None:
    with tenant_tx(office.ctx()) as conn:
        conn.execute(
            text("INSERT INTO jobs (office_id, version_id, kind, payload, idempotency_key)"
                 " VALUES (app_office(), :v, 'extract_facts', CAST(:p AS jsonb), :k)"),
            {"v": ver, "p": json.dumps({"attribute_id": str(attr)}), "k": f"extract_facts:{ver}:{attr}"},
        )


def test_structured_column_is_whitelisted(office):
    a, _, _ = office
    with pytest.raises(DBAPIError):
        with tenant_tx(a.ctx()) as conn:
            conn.execute(text(
                "INSERT INTO attribute_definitions (office_id, key, label_he, value_type, source, structured_column,"
                " status) VALUES (app_office(), 'pw', 'סיסמה', 'text', 'structured', 'users.password_hash', 'active')"
            ))
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text(
            "INSERT INTO attribute_definitions (office_id, key, label_he, value_type, unit_dimension, canonical_unit,"
            " source, structured_column, status) VALUES (app_office(), 'rooms', 'מספר חדרים', 'numeric', 'count',"
            " 'room', 'structured', 'occurrences.rooms', 'active')"
        ))
        assert conn.execute(text("SELECT facts_version FROM attribute_definitions WHERE key = 'rooms'")).scalar() == 1


def test_turn_id_is_unique_per_conversation(office):
    a, _, _ = office
    conv, _ = make_question(a, a.admin_id, "admin", "שאלה")
    insert = text("INSERT INTO questions (office_id, conversation_id, user_id, question_text, turn_id, status)"
                  " VALUES (app_office(), :c, :u, 'שאלה', '00000000-0000-0000-0000-000000000001', 'pending')")
    with tenant_tx(a.ctx()) as conn:
        conn.execute(insert, {"c": conv, "u": a.admin_id})
        assert conn.execute(text("SELECT status FROM questions WHERE turn_id IS NULL")).scalar() == "done"
    with pytest.raises(DBAPIError):
        with tenant_tx(a.ctx()) as conn:
            conn.execute(insert, {"c": conv, "u": a.admin_id})


def test_worker_fails_extract_facts_job_without_touching_the_version(office):
    a, doc, ver = office
    _enqueue_extract(a, ver, make_attribute(a))
    assert worker.run_one("w") is True
    assert worker.run_one("w") is False  # terminal: never re-claimed
    with tenant_tx(a.ctx()) as conn:
        job = conn.execute(text("SELECT status, last_error FROM jobs")).one()
        assert (job.status, job.last_error) == ("failed", "cloud_unavailable")
        assert conn.execute(text("SELECT status FROM document_versions WHERE id = :v"), {"v": ver}).scalar() == "ready"


def test_downgrade_then_upgrade_on_a_populated_database(office):
    a, doc, ver = office
    attr = make_attribute(a)
    make_fact(a, doc, ver, attr)
    make_question(a, a.admin_id, "admin", "שאלה")
    _enqueue_extract(a, ver, attr)
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("UPDATE conversations SET state = '{\"topic\": \"x\"}', state_version = 3"))
        conn.execute(text("UPDATE questions SET turn_id = gen_random_uuid(), plan = '{}'"))
        conn.execute(text("INSERT INTO jobs (office_id, version_id, kind, idempotency_key)"
                          " VALUES (app_office(), :v, 'process', :k)"), {"v": ver, "k": f"process:{ver}"})
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0003")
        reset_engine()
        with tenant_tx(a.ctx()) as conn:
            assert conn.execute(text("SELECT to_regclass('facts')")).scalar() is None
            assert conn.execute(text("SELECT kind FROM jobs")).scalars().all() == ["process"]
            assert conn.execute(text("SELECT count(*) FROM questions")).scalar() == 1
            cols = conn.execute(text("SELECT column_name FROM information_schema.columns"
                                     " WHERE table_name = 'conversations'")).scalars().all()
            assert "state" not in cols
        with tenant_tx(a.ctx()) as conn:
            claimed = conn.execute(text("SELECT * FROM jobs_claim('w', 60)")).one()
            assert "payload" not in claimed._fields and claimed.kind == "process"
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM documents")).scalar() == 1
        assert conn.execute(text("SELECT state, state_version FROM conversations")).one() == ({}, 0)
        assert conn.execute(text("SELECT count(*) FROM facts")).scalar() == 0
    attr = make_attribute(a)
    make_fact(a, doc, ver, attr)
    _enqueue_extract(a, ver, attr)


def test_seed_adds_structured_attributes_per_office_idempotently(db, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "seed_demo", Path(__file__).resolve().parents[2] / "scripts" / "seed_demo.py")
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")

    class Api:  # stands in for a logged-in admin client of the live stack
        def __init__(self, office_id):
            self.office_id = office_id

        def get(self, path):
            assert path == "/api/auth/me"
            return type("R", (), {"json": lambda _: {"office": {"id": str(self.office_id)}}})()

    monkeypatch.setenv("OWNER_DATABASE_URL", get_settings().owner_database_url)
    for _ in range(2):
        seed.seed_structured_attributes({"A": Api(a.office_id), "B": Api(b.office_id)})
    emp = make_user(a, "emp@example.test", [])
    for ctx in (a.ctx(), b.ctx(), TenantContext(a.office_id, emp, "employee")):
        with tenant_tx(ctx) as conn:
            rows = conn.execute(text("SELECT key, source, structured_column, status, value_type, aliases"
                                     " FROM attribute_definitions ORDER BY key")).all()
        assert [r.key for r in rows] == ["area", "price", "price_per_sqm", "rooms"]
        assert {(r.source, r.status, r.value_type) for r in rows} == {("structured", "active", "numeric")}
        assert dict((r.key, r.structured_column) for r in rows)["rooms"] == "occurrences.rooms"
        assert all(r.aliases for r in rows)
