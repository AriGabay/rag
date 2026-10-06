"""Office and document-group isolation enforced by PostgreSQL RLS (U2, KTD3-KTD6)."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from app.config import get_settings
from app.db import TenantContext, anonymous_tx, tenant_tx
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db

TENANT_TABLES = [
    "offices", "office_settings", "office_data_versions", "users", "document_groups", "user_groups", "sessions",
    "documents", "document_versions", "pages", "extracted_tables", "chunks", "jobs", "audit_events",
    "provider_usage", "transactions", "occurrences", "fact_values", "dedup_candidates", "conversations",
    "questions", "answer_sources", "answer_cache",
]


@pytest.fixture
def two_offices(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    make_document(a, a.default_group_id, "מסמך א", sha="a" * 64)
    make_document(b, b.default_group_id, "מסמך ב", sha="b" * 64)
    return a, b


def test_runtime_role_cannot_bypass_rls(db):
    engine = create_engine(get_settings().database_url)
    with engine.connect() as conn:
        bypass, owned = conn.execute(
            text(
                "SELECT r.rolbypassrls, (SELECT count(*) FROM pg_tables t WHERE t.tableowner = current_user)"
                " FROM pg_roles r WHERE r.rolname = current_user"
            )
        ).one()
    engine.dispose()
    assert bypass is False
    assert owned == 0


def test_each_tenant_table_shows_only_own_office(two_offices):
    a, b = two_offices
    with tenant_tx(a.ctx()) as conn:
        for table in TENANT_TABLES:
            col = "id" if table == "offices" else "office_id"
            foreign = conn.execute(text(f"SELECT count(*) FROM {table} WHERE {col} = :b"), {"b": b.office_id}).scalar()
            assert foreign == 0, table
        assert conn.execute(text("SELECT count(*) FROM documents")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 1


def test_no_context_sees_nothing(two_offices):
    with anonymous_tx() as conn:
        for table in TENANT_TABLES:
            assert conn.execute(text(f"SELECT count(*) FROM {table}")).scalar() == 0, table


def test_insert_into_other_office_is_rejected(two_offices):
    a, b = two_offices
    with pytest.raises(DBAPIError):
        with tenant_tx(a.ctx()) as conn:
            conn.execute(
                text("INSERT INTO document_groups (office_id, name) VALUES (:b, 'חדירה')"), {"b": b.office_id}
            )


def test_document_groups_limit_employee_visibility(two_offices):
    a, _ = two_offices
    g1 = make_group(a, "קבוצה 1")
    g2 = make_group(a, "קבוצה 2")
    make_document(a, g1, "מסמך ק1", sha="1" * 64)
    make_document(a, g2, "מסמך ק2", sha="2" * 64)
    emp = make_user(a, "emp1@example.test", [g1])

    def titles(ctx):
        with tenant_tx(ctx) as conn:
            return sorted(conn.execute(text("SELECT title FROM documents")).scalars())

    assert titles(TenantContext(a.office_id, emp, "employee")) == ["מסמך ק1"]
    assert titles(a.ctx()) == ["מסמך א", "מסמך ק1", "מסמך ק2"]
    assert titles(a.system) == ["מסמך א", "מסמך ק1", "מסמך ק2"]
    # child rows follow the document's visibility
    with tenant_tx(TenantContext(a.office_id, emp, "employee")) as conn:
        assert conn.execute(text("SELECT count(*) FROM document_versions")).scalar() == 1


def test_lookup_functions_work_without_context_while_tables_stay_hidden(two_offices):
    a, _ = two_offices
    with anonymous_tx() as conn:
        rows = conn.execute(text("SELECT * FROM auth_login_lookup('ADMIN-A@example.test')")).all()
        assert len(rows) == 1 and rows[0].office_id == a.office_id and rows[0].role == "admin"
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() == 0
        assert conn.execute(text("SELECT * FROM auth_resolve_session('nope')")).all() == []


def test_jobs_claim_returns_one_locked_job(two_offices):
    a, b = two_offices
    for office in (a, b):
        with tenant_tx(office.ctx()) as conn:
            ver = conn.execute(text("SELECT id FROM document_versions")).scalar_one()
            conn.execute(
                text("INSERT INTO jobs (office_id, version_id, kind, idempotency_key) VALUES (app_office(), :v, 'process', :k)"),
                {"v": ver, "k": f"process:{ver}"},
            )
    with anonymous_tx() as conn:
        first = conn.execute(text("SELECT * FROM jobs_claim('w1', 60)")).all()
        second = conn.execute(text("SELECT * FROM jobs_claim('w2', 60)")).all()
        third = conn.execute(text("SELECT * FROM jobs_claim('w3', 60)")).all()
    assert len(first) == 1 and len(second) == 1 and third == []
    assert {first[0].office_id, second[0].office_id} == {a.office_id, b.office_id}


def test_bootstrap_office_not_executable_by_runtime_role(db):
    with pytest.raises(DBAPIError):
        with anonymous_tx() as conn:
            conn.execute(text("SELECT bootstrap_office('x', 'x@x.test', 'x', 'h')"))


def test_hash_lookup_is_scoped_to_caller_office(two_offices):
    a, b = two_offices
    with tenant_tx(a.ctx()) as conn:
        assert len(conn.execute(text("SELECT * FROM docs_hash_lookup(:s)"), {"s": "a" * 64}).all()) == 1
        assert conn.execute(text("SELECT * FROM docs_hash_lookup(:s)"), {"s": "b" * 64}).all() == []
