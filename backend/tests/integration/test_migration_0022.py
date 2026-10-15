"""Migration 0022: ``version_dates.primary_section`` (round 12 U3b, KTD11; R15, R16).

The column is a boolean that is never null and defaults to false; the revision comes and goes without touching a
version_dates row, a document, a version or a conversation: downgrade drops the column and nothing else. Round 13 U13
(KTD17; AE10; R21, R22): the downgrade also *relabels* (UPDATE, never DELETE) the rows written by the versions that know
the column to ``<version>-rolled-back``, so a re-upgrade finds no current-version row with a lost flag and derives the
dates again beside them. Synthetic data only."""

from uuid import UUID

import pytest
from sqlalchemy import text

from alembic import command
from app.chat import dates as D
from app.db import reset_engine, tenant_tx
from tests.conftest import alembic_config
from tests.factories import make_document, make_office
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db


def _column(db):
    with db.connect() as conn:
        return conn.execute(text(
            "SELECT data_type, is_nullable, column_default FROM information_schema.columns"
            " WHERE table_name = 'version_dates' AND column_name = 'primary_section'")).first()


def _date_row(conn, doc, ver, n: int, dates_version: str = "dates-v2") -> None:
    conn.execute(text(
        "INSERT INTO version_dates (office_id, document_id, version_id, dates_version, block_index, iso, precision,"
        " written, status) VALUES (app_office(), :d, :v, :dv, :n, '2025-03-01', 'day', 'x', 'stated')"),
        {"d": doc, "v": ver, "dv": dates_version, "n": n})


def test_the_column_is_a_non_null_boolean_defaulting_to_false(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    col = _column(db)
    assert col is not None and (col.data_type, col.is_nullable) == ("boolean", "NO") and "false" in col.column_default
    with tenant_tx(a.ctx()) as conn:
        _date_row(conn, doc, ver, 1)
        assert conn.execute(text("SELECT primary_section FROM version_dates")).scalar_one() is False


def test_downgrade_drops_only_the_column_and_an_upgrade_again_keeps_every_row(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    with tenant_tx(a.ctx()) as conn:
        _date_row(conn, doc, ver, 1)
        conn.execute(text("UPDATE version_dates SET primary_section = true"))
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0021")
        reset_engine()
        assert _column(db) is None
        with tenant_tx(a.ctx()) as conn:
            assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 1
        with tenant_tx(a.system) as conn:
            assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 1
            assert conn.execute(text("SELECT count(*) FROM document_versions")).scalar_one() == 1
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _column(db) is not None
    with tenant_tx(a.ctx()) as conn:   # the row is kept; its flag reads false again (the dates are derived again)
        assert conn.execute(text("SELECT count(*), bool_or(primary_section) FROM version_dates")).one() == (1, False)


# --- round 13 U13: the downgrade relabels instead of leaving stale flags (KTD17; AE10; R21, R22) ----------------------

SUFFIX = "-rolled-back"


def _versions(office) -> list[tuple[str, int]]:
    """(dates_version, rows) over the whole table, as the office sees it."""
    with tenant_tx(office.system) as conn:
        return [(r[0], r[1]) for r in conn.execute(text(
            "SELECT dates_version, count(*) FROM version_dates GROUP BY dates_version ORDER BY dates_version"))]


def _other_counts(office) -> dict[str, int]:
    tables = ("documents", "document_versions", "document_blocks", "document_units")
    with tenant_tx(office.system) as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables}


def _rls(db) -> tuple[bool, bool]:
    with db.connect() as conn:
        return conn.execute(text(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE relname = 'version_dates'")).one()


def _appraisal(office):
    """A one-unit appraisal whose valuation date sits in a block that begins with the label: its date is primary."""
    return unit_file(office, office.default_group_id, "שומה סינתטית",
                     [{"title": "שומה סינתטית", "blocks": ["שומה סינתטית — נתונים בדויים", "המועד הקובע: 15/04/2024",
                                                           "שווי הנכס 2,450,000 ₪"]}])


def _seed(db, office):
    """Current-version rows derived for real (primary), plus injected rows of dates-v1, dates-v2 and dates-v3."""
    doc, ver = _appraisal(office)
    with tenant_tx(office.system) as conn:
        got = D.valuation_dates(conn, [ver])[UUID(ver)]
    assert [(d.iso, d.primary) for d in got] == [("2024-04-15", True)]
    with tenant_tx(office.system) as conn:
        _date_row(conn, doc, UUID(ver), 7, "dates-v1")
        _date_row(conn, doc, UUID(ver), 8, "dates-v2")
        _date_row(conn, doc, UUID(ver), 9, "dates-v2")
        _date_row(conn, doc, UUID(ver), 10, "dates-v3")
        conn.execute(text("UPDATE version_dates SET primary_section = true WHERE dates_version = 'dates-v3'"))
    return doc, ver


def test_ae10_the_downgrade_relabels_only_the_rows_of_versions_that_know_the_column_and_deletes_none(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    _seed(db, a)
    before, others = _versions(a), _other_counts(a)
    assert before == [("dates-v1", 1), ("dates-v2", 2), ("dates-v3", 1), (D.DATES_VERSION, 1)]
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0021")
        reset_engine()
        assert _column(db) is None
        after = _versions(a)
        assert after == [("dates-v1", 1), ("dates-v2", 2), ("dates-v3" + SUFFIX, 1), (D.DATES_VERSION + SUFFIX, 1)]
        assert sum(n for _, n in after) == sum(n for _, n in before)           # no row removed
        assert sum(n for v, n in after if v.endswith(SUFFIX)) == 2             # = the rows of the relabelled versions
        assert _rls(db) == (True, True)
        assert _other_counts(a) == others
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _column(db) is not None
    assert _versions(a) == after and _rls(db) == (True, True) and _other_counts(a) == others


def test_the_first_read_after_a_cycle_derives_fresh_primary_rows_beside_the_relabelled_ones(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    _, ver = _seed(db, a)
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0021")
        reset_engine()
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    with tenant_tx(a.system) as conn:   # the re-added column reads false on every old row: those rows must not be read
        assert conn.execute(text("SELECT bool_or(primary_section) FROM version_dates")).scalar_one() is False
    with tenant_tx(a.system) as conn:
        got = D.valuation_dates(conn, [ver])[UUID(ver)]
    assert [(d.iso, d.primary, d.status) for d in got] == [("2024-04-15", True, "stated")]
    assert _versions(a) == [("dates-v1", 1), ("dates-v2", 2), ("dates-v3" + SUFFIX, 1),
                            (D.DATES_VERSION, 1), (D.DATES_VERSION + SUFFIX, 1)]
    with tenant_tx(a.system) as conn:   # the fresh row carries its flag; the relabelled one still reads false
        flags = dict(conn.execute(text("SELECT dates_version, bool_or(primary_section) FROM version_dates"
                                       " WHERE dates_version LIKE 'dates-v%' GROUP BY dates_version")).all())
    assert flags[D.DATES_VERSION] is True and flags[D.DATES_VERSION + SUFFIX] is False


def test_two_cycles_keep_every_row_and_never_double_the_suffix(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    _, ver = _seed(db, a)
    others = _other_counts(a)
    cfg = alembic_config()
    try:
        for _ in range(2):
            command.downgrade(cfg, "0021")
            reset_engine()
            assert _rls(db) == (True, True)
            command.upgrade(cfg, "head")
            reset_engine()
            with tenant_tx(a.system) as conn:   # the read between the cycles derives the current rows again
                D.valuation_dates(conn, [ver])
        command.downgrade(cfg, "0021")          # a third downgrade, now with relabelled rows already present
        reset_engine()
        got = _versions(a)
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert not any(v.endswith(SUFFIX + SUFFIX) for v, _ in got)
    assert got == [("dates-v1", 1), ("dates-v2", 2), ("dates-v3" + SUFFIX, 1), (D.DATES_VERSION + SUFFIX, 3)]
    assert sum(n for _, n in got) == 7        # 5 seeded rows' worth + the 2 re-derived: none was lost
    assert _other_counts(a) == others and _rls(db) == (True, True)
