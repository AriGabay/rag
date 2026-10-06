"""Office, document-group and per-user isolation enforced by PostgreSQL RLS (U2, KTD3-KTD6; U3, KTD6)."""

import json
from uuid import UUID

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
    "questions", "answer_sources", "answer_cache", "attribute_definitions", "facts", "fact_extraction_ledger",
]


def make_attribute(office, key: str = "balcony_area", label: str = "שטח מרפסת") -> UUID:
    with tenant_tx(office.ctx()) as conn:
        return conn.execute(
            text(
                "INSERT INTO attribute_definitions (office_id, key, label_he, value_type, unit_dimension, canonical_unit,"
                " source, extraction_prompt_version, status)"
                " VALUES (app_office(), :k, :l, 'numeric', 'area', 'sqm', 'extracted', 'v1', 'active') RETURNING id"
            ),
            {"k": key, "l": label},
        ).scalar_one()


def make_fact(office, doc: UUID, ver: UUID, attr: UUID, value: str = "12.5", state: str = "found") -> UUID:
    """One fact plus its ledger entry, written as the office admin."""
    with tenant_tx(office.ctx()) as conn:
        fact_id = conn.execute(
            text(
                "INSERT INTO facts (office_id, document_id, version_id, attribute_id, entity_role, value_numeric, unit,"
                " canonical_value, quote, source_path, extraction_version, model, status)"
                " VALUES (app_office(), :d, :v, :a, 'subject', :n, 'מ״ר', :n, :q, CAST(:sp AS jsonb), 'v1', 'stub',"
                " 'auto_validated') RETURNING id"
            ),
            {"d": doc, "v": ver, "a": attr, "n": value, "q": f"שטח המרפסת {value} מ״ר",
             "sp": json.dumps({"page": 1, "chunk_id": None})},
        ).scalar_one()
        conn.execute(
            text(
                "INSERT INTO fact_extraction_ledger (office_id, document_id, version_id, attribute_id,"
                " extraction_version, state, char_budget) VALUES (app_office(), :d, :v, :a, 'v1', :s, 40000)"
            ),
            {"d": doc, "v": ver, "a": attr, "s": state},
        )
    return fact_id


def make_question(office, user_id: UUID, role: str, text_: str) -> tuple[UUID, UUID]:
    with tenant_tx(office.ctx(role, user_id)) as conn:
        conv = conn.execute(
            text("INSERT INTO conversations (office_id, user_id, title) VALUES (app_office(), :u, :t) RETURNING id"),
            {"u": user_id, "t": text_},
        ).scalar_one()
        qid = conn.execute(
            text(
                "INSERT INTO questions (office_id, conversation_id, user_id, question_text)"
                " VALUES (app_office(), :c, :u, :q) RETURNING id"
            ),
            {"c": conv, "u": user_id, "q": text_},
        ).scalar_one()
    return conv, qid


@pytest.fixture
def two_offices(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    for office, title, sha in ((a, "מסמך א", "a"), (b, "מסמך ב", "b")):
        doc, ver = make_document(office, office.default_group_id, title, sha=sha * 64)
        make_fact(office, doc, ver, make_attribute(office))
        make_question(office, office.admin_id, "admin", f"שאלה של {title}")
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


def test_every_tenant_table_has_forced_rls(db):
    with db.connect() as conn:
        rows = conn.execute(
            text("SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = ANY(:t)"),
            {"t": TENANT_TABLES},
        ).all()
    assert {r.relname for r in rows} == set(TENANT_TABLES)
    assert all(r.relrowsecurity and r.relforcerowsecurity for r in rows), [r.relname for r in rows]


def test_new_tables_show_only_own_office_rows(two_offices):
    a, b = two_offices
    for office, other in ((a, b), (b, a)):
        with tenant_tx(office.ctx()) as conn:
            for table in ("attribute_definitions", "facts", "fact_extraction_ledger"):
                rows = conn.execute(text(f"SELECT office_id FROM {table}")).scalars().all()
                assert rows == [office.office_id], table
            assert conn.execute(
                text("SELECT count(*) FROM facts f JOIN attribute_definitions ad ON ad.id = f.attribute_id"
                     " WHERE ad.office_id = :o"), {"o": other.office_id}
            ).scalar() == 0


def test_facts_from_a_hidden_group_are_invisible_to_employee(two_offices):
    a, _ = two_offices
    g1, g2 = make_group(a, "קבוצה 1"), make_group(a, "קבוצה 2")
    attr = make_attribute(a, "storage_area", "שטח מחסן")
    seen_doc, seen_ver = make_document(a, g1, "גלוי", sha="1" * 64)
    hidden_doc, hidden_ver = make_document(a, g2, "מוסתר", sha="2" * 64)
    make_fact(a, seen_doc, seen_ver, attr, "6")
    hidden_fact = make_fact(a, hidden_doc, hidden_ver, attr, "9", state="found")
    emp = make_user(a, "emp-facts@example.test", [g1])

    def visible(ctx):
        with tenant_tx(ctx) as conn:
            facts = conn.execute(text("SELECT id, value_numeric FROM facts WHERE attribute_id = :a"), {"a": attr}).all()
            ledger = conn.execute(
                text("SELECT version_id FROM fact_extraction_ledger WHERE attribute_id = :a"), {"a": attr}
            ).scalars().all()
            defs = conn.execute(text("SELECT count(*) FROM attribute_definitions WHERE id = :a"), {"a": attr}).scalar()
        return facts, ledger, defs

    facts, ledger, defs = visible(TenantContext(a.office_id, emp, "employee"))
    assert [str(f.value_numeric) for f in facts] == ["6"] and ledger == [seen_ver] and defs == 1
    facts, ledger, _ = visible(a.ctx())
    assert hidden_fact in {f.id for f in facts} and set(ledger) == {seen_ver, hidden_ver}
    with tenant_tx(TenantContext(a.office_id, emp, "employee")) as conn:  # nor can the employee touch it
        assert conn.execute(text("UPDATE facts SET status = 'verified' WHERE id = :f RETURNING id"),
                            {"f": hidden_fact}).first() is None


def test_conversations_and_questions_are_private_to_their_user(two_offices):
    a, _ = two_offices
    emp1 = make_user(a, "emp-x@example.test", [a.default_group_id])
    emp2 = make_user(a, "emp-y@example.test", [a.default_group_id])
    x_conv, x_q = make_question(a, emp1, "employee", "שאלה של X")
    make_question(a, emp2, "employee", "שאלה של Y")

    def mine(user_id, role):
        with tenant_tx(a.ctx(role, user_id)) as conn:
            convs = conn.execute(text("SELECT title FROM conversations")).scalars().all()
            qs = conn.execute(text("SELECT question_text FROM questions")).scalars().all()
        return convs, qs

    assert mine(emp1, "employee") == (["שאלה של X"], ["שאלה של X"])
    assert mine(emp2, "employee") == (["שאלה של Y"], ["שאלה של Y"])
    assert mine(a.admin_id, "admin") == (["שאלה של מסמך א"], ["שאלה של מסמך א"])  # admins included
    with tenant_tx(a.ctx()) as conn:  # the admin can neither change nor delete another user's rows
        assert conn.execute(text("UPDATE questions SET question_text = 'x' WHERE id = :q RETURNING id"),
                            {"q": x_q}).first() is None
        assert conn.execute(text("DELETE FROM conversations WHERE id = :c RETURNING id"), {"c": x_conv}).first() is None
    with pytest.raises(DBAPIError):  # nor write a row on another user's behalf
        with tenant_tx(a.ctx()) as conn:
            conn.execute(text("INSERT INTO conversations (office_id, user_id) VALUES (app_office(), :u)"), {"u": emp1})
    with tenant_tx(a.system) as conn:  # the server-only system context keeps office-wide access
        assert conn.execute(text("SELECT count(*) FROM questions")).scalar() == 3
        assert conn.execute(text("SELECT count(*) FROM conversations")).scalar() == 3


def _insert_job(office, kind: str, payload: dict | None = None, created: str = "now()") -> UUID:
    with tenant_tx(office.ctx()) as conn:
        ver = conn.execute(text("SELECT id FROM document_versions ORDER BY created_at LIMIT 1")).scalar_one()
        return conn.execute(
            text(
                "INSERT INTO jobs (office_id, version_id, kind, payload, idempotency_key, created_at)"
                f" VALUES (app_office(), :v, :k, CAST(:p AS jsonb), :ik, {created}) RETURNING id"
            ),
            {"v": ver, "k": kind, "p": json.dumps(payload) if payload is not None else None,
             "ik": f"{kind}:{ver}:{json.dumps(payload)}"},
        ).scalar_one()


def test_jobs_claim_returns_kind_and_payload_and_claims_process_first(two_offices):
    a, b = two_offices
    attr = str(make_attribute(a, "floor", "קומה"))
    old_extract = _insert_job(a, "extract_facts", {"attribute_id": attr}, created="now() - interval '1 hour'")
    process = _insert_job(b, "process")
    with anonymous_tx() as conn:
        first = conn.execute(text("SELECT * FROM jobs_claim('w1', 60)")).one()
        second = conn.execute(text("SELECT * FROM jobs_claim('w2', 60)")).one()
    assert (first.job_id, first.kind, first.payload) == (process, "process", None)
    assert (second.job_id, second.kind, second.payload) == (old_extract, "extract_facts", {"attribute_id": attr})


def test_jobs_claim_fails_exhausted_jobs_and_only_process_jobs_fail_their_version(two_offices):
    a, b = two_offices
    extract = _insert_job(a, "extract_facts", {"attribute_id": str(make_attribute(a, "floor", "קומה"))})
    process = _insert_job(b, "process")
    for office in (a, b):  # both workers died on their last allowed attempt
        with tenant_tx(office.ctx()) as conn:
            conn.execute(text("UPDATE jobs SET status = 'running', attempts = max_attempts,"
                              " lease_until = now() - interval '1 second'"))
            conn.execute(text("UPDATE document_versions SET status = 'processing'"))
    with anonymous_tx() as conn:
        assert conn.execute(text("SELECT * FROM jobs_claim('w', 60)")).all() == []
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT status FROM jobs WHERE id = :j"), {"j": extract}).scalar() == "failed"
        assert conn.execute(text("SELECT status FROM document_versions")).scalar() == "processing"
    with tenant_tx(b.ctx()) as conn:
        assert conn.execute(text("SELECT status FROM jobs WHERE id = :j"), {"j": process}).scalar() == "failed"
        assert conn.execute(text("SELECT status FROM document_versions")).scalar() == "failed"
