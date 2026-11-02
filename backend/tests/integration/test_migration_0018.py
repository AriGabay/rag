"""Migration 0018: ``document_units`` comes and goes cleanly; row security stays forced; a unit is reachable only
through a document the user may see, never from another office; a unit is unique per version, reading and number;
and its kind and certainty are constrained. Synthetic data only."""

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


def _unit(conn, doc, ver, no: int = 1, kind: str = "appraisal", certainty: str = "certain", reading: str = "reading-1",
          aliases: str = "{שדרות האלון,האלון}") -> None:
    conn.execute(text(
        "INSERT INTO document_units (office_id, document_id, version_id, reading_id, unit_no, kind, title, aliases,"
        " identifiers, page_first, page_last, block_first, block_last, certainty, evidence, units_version,"
        " derivation_key) VALUES (app_office(), :d, :v, :r, :n, :k, 'פרויקט שדרות האלון', CAST(:a AS text[]),"
        " CAST('{\"parcels\": [[\"31102\", \"7\"]]}' AS jsonb), 1, 2, 0, 17, :c,"
        " CAST('{\"boundary\": {\"rule\": \"file_start\"}}' AS jsonb), 'units-v1', 'units-v1|-')"),
        {"d": doc, "v": ver, "r": reading, "n": no, "k": kind, "a": aliases, "c": certainty})


def test_the_revision_comes_and_goes_and_keeps_row_security_forced(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    assert _exists(db, "document_units") and _forced(db, "document_units")
    with db.connect() as conn:
        indexes = {r.indexname: r.indexdef for r in conn.execute(text(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'document_units'"))}
    assert any("USING gin (aliases)" in d for d in indexes.values())
    with tenant_tx(a.system) as conn:
        _unit(conn, doc, ver)
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0017")
        reset_engine()
        assert not _exists(db, "document_units")
        assert _forced(db, "region_readings") and _forced(db, "verified_values")  # the older tables are untouched
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _exists(db, "document_units") and _forced(db, "document_units")
    with tenant_tx(a.system) as conn:
        assert conn.execute(text("SELECT count(*) FROM document_units")).scalar_one() == 0  # derived again on first use


def test_a_unit_follows_the_permissions_of_its_document(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    closed = make_group(a, "קבוצה סגורה")
    inside = make_user(a, "inside@example.test", [a.default_group_id, closed])
    outside = make_user(a, "outside@example.test", [a.default_group_id])
    doc, ver = make_document(a, closed, title="שומה סינתטית סגורה")
    with tenant_tx(a.ctx("employee", inside)) as conn:  # a user who may see the document stores and reads its units
        _unit(conn, doc, ver, 1)
        _unit(conn, doc, ver, 2, kind="appendix", certainty="probable", aliases="{}")
        rows = conn.execute(text("SELECT unit_no, kind, aliases FROM document_units ORDER BY unit_no")).all()
        assert [(r.unit_no, r.kind) for r in rows] == [(1, "appraisal"), (2, "appendix")]
        assert rows[0].aliases == ["שדרות האלון", "האלון"]
        found = conn.execute(text("SELECT unit_no FROM document_units WHERE aliases && ARRAY['האלון']")).scalars().all()
        assert found == [1]
    with tenant_tx(a.ctx("employee", outside)) as conn:  # outside the group they do not exist
        assert conn.execute(text("SELECT count(*) FROM document_units")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(a.ctx("employee", outside)) as conn:  # nor can they be written
        _unit(conn, doc, ver, 3)
    with tenant_tx(b.ctx()) as conn:  # another office sees nothing, even as its admin
        assert conn.execute(text("SELECT count(*) FROM document_units")).scalar_one() == 0
        assert conn.execute(text("SELECT count(*) FROM document_units WHERE aliases && ARRAY['האלון']")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(b.ctx()) as conn:  # nor write one against office A's document
        _unit(conn, doc, ver, 4)
    with tenant_tx(a.ctx()) as conn:  # the office's admin sees both
        assert conn.execute(text("SELECT count(*) FROM document_units")).scalar_one() == 2


def test_units_are_unique_per_version_reading_and_number_and_their_kind_and_certainty_are_constrained(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    with tenant_tx(a.ctx()) as conn:
        _unit(conn, doc, ver, 1)
        _unit(conn, doc, ver, 1, reading="reading-2")  # a replaced reading keeps its own numbering
        with pytest.raises(DBAPIError), conn.begin_nested():
            _unit(conn, doc, ver, 1)
        with pytest.raises(DBAPIError), conn.begin_nested():
            _unit(conn, doc, ver, 2, kind="chapter")
        with pytest.raises(DBAPIError), conn.begin_nested():
            _unit(conn, doc, ver, 2, certainty="sure")
        with pytest.raises(DBAPIError), conn.begin_nested():
            _unit(conn, doc, ver, 0)
    with tenant_tx(a.ctx()) as conn:  # deleting the document removes its units
        conn.execute(text("DELETE FROM document_versions WHERE id = :v"), {"v": ver})
        assert conn.execute(text("SELECT count(*) FROM document_units")).scalar_one() == 0
