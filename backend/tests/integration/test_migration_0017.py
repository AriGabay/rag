"""Migration 0017: ``trigram_candidates`` comes and goes cleanly, is owned by the BYPASSRLS lookup role with a pinned
search path, and only the runtime role may run it (round 9 U2, R16, KTD3). Synthetic data only."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_office

pytestmark = pytest.mark.db

CALL = "SELECT * FROM trigram_candidates('שיקולי השמאי', CAST(:v AS uuid[]), CAST(0.35 AS real), 5)"


def _function(db):
    with db.connect() as conn:
        return conn.execute(text(
            "SELECT pg_get_userbyid(proowner) AS owner, prosecdef, proconfig, proacl::text AS acl FROM pg_proc"
            " WHERE proname = 'trigram_candidates'")).one_or_none()


def _chunk_columns_of_lookup(db) -> set[str]:
    with db.connect() as conn:
        return {r.column_name for r in conn.execute(text(
            "SELECT column_name FROM information_schema.column_privileges WHERE table_name = 'chunks'"
            " AND grantee = 'rag_lookup' AND privilege_type = 'SELECT'"))}


def test_the_function_is_a_definer_lookup_owned_by_the_bypass_role_for_the_app_role_only(db):
    fn = _function(db)
    assert fn.owner == "rag_lookup" and fn.prosecdef
    assert any(c.startswith("search_path=") and "pg_temp" in c for c in fn.proconfig)
    # PUBLIC has no entry (an empty grantee would read "=X/..."), the owner and the runtime role do
    entries = {e.split("=")[0] for e in fn.acl.strip("{}").split(",")}
    assert entries == {"rag_lookup", "rag_app"}
    with db.connect() as conn:
        assert conn.execute(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'rag_lookup'")).scalar_one()
        assert not conn.execute(text("SELECT rolbypassrls FROM pg_roles WHERE rolname = 'rag_app'")).scalar_one()
        assert conn.execute(text("SELECT has_function_privilege('rag_app',"
                                 " 'trigram_candidates(text, uuid[], real, integer)', 'EXECUTE')")).scalar_one()
        assert not conn.execute(text("SELECT has_function_privilege('public',"
                                     " 'trigram_candidates(text, uuid[], real, integer)', 'EXECUTE')")).scalar_one()
    assert _chunk_columns_of_lookup(db) >= {"id", "office_id", "version_id", "normalized_text"}
    assert "text" not in _chunk_columns_of_lookup(db)  # the lookup role never reads the passage text


def test_the_function_runs_for_the_app_role_inside_its_office_and_never_without_one(db):
    a = make_office(db, "משרד א", "a@example.test")
    b = make_office(db, "משרד ב", "b@example.test")
    _, ver_a = make_document(a, a.default_group_id, "שיקולי השמאי א", sha="a" * 64)
    _, ver_b = make_document(b, b.default_group_id, "שיקולי השמאי ב", sha="b" * 64)
    for office, ver, body in ((a, ver_a, "4. שיקולי השמאי: הפחתה"), (b, ver_b, "4. שיקולי השמאי: תוספת")):
        with tenant_tx(office.system) as conn:
            conn.execute(text(
                "INSERT INTO chunks (office_id, document_id, version_id, chunk_index, kind, text, normalized_text)"
                " SELECT app_office(), document_id, id, 0, 'text', :t, :t FROM document_versions WHERE id = :v"),
                {"t": body, "v": ver})
    with tenant_tx(a.ctx()) as conn:
        assert len(conn.execute(text(CALL), {"v": [ver_a]}).all()) == 1
        assert conn.execute(text(CALL), {"v": [ver_b]}).all() == []  # another office's version: nothing
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("SELECT set_config('app.office_id', '', true)"))  # no office context: nothing either
        assert conn.execute(text(CALL), {"v": [ver_a, ver_b]}).all() == []


def test_the_revision_comes_and_goes(db):
    assert _function(db) is not None
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0016")
        reset_engine()
        assert _function(db) is None and "normalized_text" not in _chunk_columns_of_lookup(db)
        with pytest.raises(DBAPIError), db.begin() as conn:
            conn.execute(text(CALL), {"v": []})
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _function(db).owner == "rag_lookup" and "normalized_text" in _chunk_columns_of_lookup(db)
