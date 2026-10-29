"""Migration 0019: ``documents.source_kind`` and the tables of the professional sources (round 10 U2, KTD8, KTD9, KTD10).

The column defaults to ``appraisal`` (an existing document reads as one, nothing backfilled) and is constrained to its
three kinds; ``document_sections`` and ``standard_profiles`` keep row security forced, follow the permissions of their
document, are unique per version and reading, and go with their version; the revision comes and goes without touching
a document row. Synthetic data only."""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_group, make_office, make_user

pytestmark = pytest.mark.db

TABLES = ("document_sections", "standard_profiles")


def _exists(db, table: str) -> bool:
    with db.connect() as conn:
        return conn.execute(text("SELECT to_regclass(:t) IS NOT NULL"), {"t": table}).scalar_one()


def _forced(db, table: str) -> bool:
    with db.connect() as conn:
        return bool(conn.execute(text("SELECT relforcerowsecurity AND relrowsecurity FROM pg_class"
                                      " WHERE relname = :t"), {"t": table}).scalar())


def _section(conn, doc, ver, no: int = 1, reading: str = "reading-1", role: str = "section") -> None:
    conn.execute(text(
        "INSERT INTO document_sections (office_id, document_id, version_id, reading_id, sections_version, section_no,"
        " label, title, parent_no, depth, role, block_first, block_last, page_first, page_last, limits, refs, modality,"
        " evidence) VALUES (app_office(), :d, :v, :r, 'sections-v1', :n, '3.2', 'סעיף סינתטי', NULL, 1, :role, 0, 4, 1, 2,"
        " CAST('[]' AS jsonb), CAST('[]' AS jsonb), CAST('{}' AS jsonb), CAST('{}' AS jsonb))"),
        {"d": doc, "v": ver, "r": reading, "n": no, "role": role})


def _profile(conn, doc, ver, reading: str = "reading-1") -> None:
    conn.execute(text(
        "INSERT INTO standard_profiles (office_id, document_id, version_id, reading_id, profile_version, fields)"
        " VALUES (app_office(), :d, :v, :r, 'profile-v1', CAST('{}' AS jsonb))"), {"d": doc, "v": ver, "r": reading})


def test_the_revision_comes_and_goes_keeps_row_security_forced_and_leaves_documents_alone(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    std, std_ver = make_document(a, a.default_group_id, title="תקן סינתטי", sha="s" * 64, source_kind="standard")
    for table in TABLES:
        assert _exists(db, table) and _forced(db, table)
    with db.connect() as conn:
        indexes = {r.indexname for r in conn.execute(text(
            "SELECT indexname FROM pg_indexes WHERE tablename IN ('document_sections', 'standard_profiles')"))}
    assert any("office" in i and "sections" in i for i in indexes) and any("office" in i and "profiles" in i for i in indexes)
    with tenant_tx(a.system) as conn:
        _section(conn, std, std_ver)
        _profile(conn, std, std_ver)
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0018")
        reset_engine()
        assert not any(_exists(db, t) for t in TABLES)
        with db.connect() as conn:
            assert not conn.execute(text("SELECT count(*) FROM information_schema.columns"
                                         " WHERE table_name = 'documents' AND column_name = 'source_kind'")).scalar_one()
        with tenant_tx(a.system) as conn:  # the documents' rows are intact (the kind of a document is not kept below 0019)
            assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 2
            assert conn.execute(text("SELECT count(*) FROM document_versions")).scalar_one() == 2
        assert _forced(db, "document_units") and _forced(db, "documents")  # the older tables are untouched
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    for table in TABLES:
        assert _exists(db, table) and _forced(db, table)
    with tenant_tx(a.system) as conn:  # existing documents read as appraisals (nothing backfilled), derivations come again
        assert conn.execute(text("SELECT source_kind FROM documents WHERE id = :d"), {"d": doc}).scalar_one() == "appraisal"
        assert conn.execute(text("SELECT source_kind FROM documents WHERE id = :d"), {"d": std}).scalar_one() == "appraisal"
        assert conn.execute(text("SELECT count(*) FROM document_sections")).scalar_one() == 0


def test_the_kind_defaults_to_appraisal_and_is_constrained(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    with tenant_tx(a.ctx()) as conn:
        kind = conn.execute(text("INSERT INTO documents (office_id, group_id, title)"
                                 " VALUES (app_office(), :g, 'מסמך') RETURNING source_kind"),
                            {"g": a.default_group_id}).scalar_one()
        assert kind == "appraisal"
        for ok in ("appraisal", "standard", "professional"):
            conn.execute(text("INSERT INTO documents (office_id, group_id, title, source_kind)"
                              " VALUES (app_office(), :g, 'מסמך', :k)"), {"g": a.default_group_id, "k": ok})
        for bad in ("Standard", "guide", ""):
            with pytest.raises(DBAPIError), conn.begin_nested():
                conn.execute(text("INSERT INTO documents (office_id, group_id, title, source_kind)"
                                  " VALUES (app_office(), :g, 'מסמך', :k)"), {"g": a.default_group_id, "k": bad})
        with pytest.raises(DBAPIError), conn.begin_nested():
            conn.execute(text("INSERT INTO documents (office_id, group_id, title, source_kind)"
                              " VALUES (app_office(), :g, 'מסמך', NULL)"), {"g": a.default_group_id})


def test_sections_and_profiles_follow_the_permissions_of_their_document(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    closed = make_group(a, "קבוצה סגורה")
    inside = make_user(a, "inside@example.test", [a.default_group_id, closed])
    outside = make_user(a, "outside@example.test", [a.default_group_id])
    doc, ver = make_document(a, closed, title="תקן סינתטי סגור", source_kind="standard")
    with tenant_tx(a.ctx("employee", inside)) as conn:
        _section(conn, doc, ver, 1)
        _section(conn, doc, ver, 2, role="definitions")
        _profile(conn, doc, ver)
        assert conn.execute(text("SELECT count(*) FROM document_sections")).scalar_one() == 2
        assert conn.execute(text("SELECT count(*) FROM standard_profiles")).scalar_one() == 1
    with tenant_tx(a.ctx("employee", outside)) as conn:  # outside the group they do not exist
        assert conn.execute(text("SELECT count(*) FROM document_sections")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM standard_profiles")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(a.ctx("employee", outside)) as conn:
        _section(conn, doc, ver, 3)
    with pytest.raises(DBAPIError), tenant_tx(a.ctx("employee", outside)) as conn:
        _profile(conn, doc, ver, "reading-2")
    with tenant_tx(b.ctx()) as conn:  # another office sees nothing, even as its admin, and cannot write against it
        assert conn.execute(text("SELECT count(*) FROM document_sections")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM standard_profiles")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(b.ctx()) as conn:
        _section(conn, doc, ver, 4)
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM document_sections")).scalar_one() == 2


def test_rows_are_unique_per_version_and_reading_and_go_with_their_version(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="תקן סינתטי", source_kind="standard")
    with tenant_tx(a.ctx()) as conn:
        _section(conn, doc, ver, 1)
        _section(conn, doc, ver, 1, reading="reading-2")  # a replaced reading keeps its own numbering
        _profile(conn, doc, ver)
        _profile(conn, doc, ver, "reading-2")
        with pytest.raises(DBAPIError), conn.begin_nested():
            _section(conn, doc, ver, 1)
        with pytest.raises(DBAPIError), conn.begin_nested():
            _profile(conn, doc, ver)
        with pytest.raises(DBAPIError), conn.begin_nested():
            _section(conn, doc, ver, 0)
        with pytest.raises(DBAPIError), conn.begin_nested():
            _section(conn, doc, ver, 2, role="verse")
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("DELETE FROM document_versions WHERE id = :v"), {"v": ver})
        assert conn.execute(text("SELECT count(*) FROM document_sections")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM standard_profiles")).scalar_one() == 0
