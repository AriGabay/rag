"""Migration 0023: units beside each other by derivation, and ``unit_cards`` (round 15 U3, KTD5, KTD6; R7; KD8).

``document_units`` is keyed by the derivation as well, so a second derivation of a version lives beside the first;
``unit_cards`` keeps row security forced, follows the permissions of its document and goes with its version; ``jobs``
accepts the ``unit_cards`` kind. The revision comes and goes without deleting a document, version, unit, date, identity
or conversation row: the downgrade drops ``unit_cards``, removes the queued ``unit_cards`` jobs (the older check cannot
hold them) and *relabels*, never deletes, the unit rows that the old key cannot hold. Synthetic data only."""

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


def _pk(db, table: str = "document_units") -> list[str]:
    """The columns of the table's primary key, in key order."""
    with db.connect() as conn:
        return [r[0] for r in conn.execute(text(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)"
            " WHERE i.indrelid = CAST(:t AS regclass) AND i.indisprimary"
            " ORDER BY array_position(i.indkey::int[], a.attnum::int)"), {"t": table})]


def _unit(conn, doc, ver, no: int, key: str = "units-v4|-", title: str = "יחידה", reading: str = "reading-1",
          created: str = "now()") -> None:
    conn.execute(text(
        "INSERT INTO document_units (office_id, document_id, version_id, reading_id, unit_no, kind, title, block_first,"
        " block_last, certainty, units_version, derivation_key, created_at) VALUES (app_office(), :d, :v, :r, :n,"
        f" 'appraisal', :t, 0, 1, 'certain', split_part(:k, '|', 1), :k, {created})"),
        {"d": doc, "v": ver, "r": reading, "n": no, "k": key, "t": title})


def _card(conn, doc, ver, first: int = 0, last: int = 3, version: str = "card-v1", state: str = "done") -> None:
    conn.execute(text(
        "INSERT INTO unit_cards (office_id, document_id, version_id, reading_id, block_first, block_last, card_version,"
        " state, fields) VALUES (app_office(), :d, :v, 'reading-1', :a, :z, :cv, :s,"
        " CAST('[{\"field\": \"רחוב הדוגמה 3\", \"quote\": \"רחוב הדוגמה 3\", \"block_index\": 1}]' AS jsonb))"),
        {"d": doc, "v": ver, "a": first, "z": last, "cv": version, "s": state})


def _job(conn, ver, kind: str, key: str) -> None:
    conn.execute(text("INSERT INTO jobs (office_id, version_id, kind, status, idempotency_key)"
                      " VALUES (app_office(), :v, :k, 'queued', :i)"), {"v": ver, "k": kind, "i": key})


def test_upgrade_re_keys_the_units_and_creates_the_cards_with_forced_row_security(db):
    assert _pk(db) == ["version_id", "reading_id", "derivation_key", "unit_no"]
    assert _exists(db, "unit_cards") and _forced(db, "unit_cards") and _forced(db, "document_units")
    assert _pk(db, "unit_cards") == ["version_id", "reading_id", "block_first", "block_last", "card_version"]
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    with tenant_tx(a.ctx()) as conn:
        _unit(conn, doc, ver, 1)
        _unit(conn, doc, ver, 1, key="units-v5|-")   # the same unit number of another derivation lives beside it
        assert conn.execute(text("SELECT count(*) FROM document_units")).scalar_one() == 2
        with pytest.raises(DBAPIError), conn.begin_nested():   # the same key and number is still one row
            _unit(conn, doc, ver, 1, key="units-v5|-")
        _job(conn, ver, "unit_cards", "unit_cards:x")   # the job kind is accepted
        with pytest.raises(DBAPIError), conn.begin_nested():
            _job(conn, ver, "no_such_kind", "k2")


def test_the_cards_are_constrained_follow_the_permissions_of_their_document_and_go_with_their_version(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    closed = make_group(a, "קבוצה סגורה")
    inside = make_user(a, "inside@example.test", [a.default_group_id, closed])
    outside = make_user(a, "outside@example.test", [a.default_group_id])
    doc, ver = make_document(a, closed, title="שומה סינתטית סגורה")
    with tenant_tx(a.ctx("employee", inside)) as conn:
        _card(conn, doc, ver, 0, 3)
        _card(conn, doc, ver, 4, 9, state="pending")
        assert conn.execute(text("SELECT count(*) FROM unit_cards")).scalar_one() == 2
        with pytest.raises(DBAPIError), conn.begin_nested():   # the state is one of the four
            conn.execute(text("UPDATE unit_cards SET state = 'guessed' WHERE block_first = 0"))
        with pytest.raises(DBAPIError), conn.begin_nested():   # a card of the same unit and card version is one row
            _card(conn, doc, ver, 0, 3)
        with pytest.raises(DBAPIError), conn.begin_nested():   # a range runs forward
            _card(conn, doc, ver, 9, 4, version="card-v2")
    with tenant_tx(a.ctx("employee", outside)) as conn:   # a user of another group sees no card of the hidden document
        assert conn.execute(text("SELECT count(*) FROM unit_cards")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(a.ctx("employee", outside)) as conn:
        _card(conn, doc, ver, 20, 30)
    with tenant_tx(b.ctx()) as conn:   # another office sees nothing, even as its admin, and cannot write against it
        assert conn.execute(text("SELECT count(*) FROM unit_cards")).scalar_one() == 0
    with pytest.raises(DBAPIError), tenant_tx(b.ctx()) as conn:
        _card(conn, doc, ver, 40, 50)
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM unit_cards")).scalar_one() == 2
        conn.execute(text("DELETE FROM document_versions WHERE id = :v"), {"v": ver})
        assert conn.execute(text("SELECT count(*) FROM unit_cards")).scalar_one() == 0


def test_the_revision_comes_and_goes_on_an_empty_database(db):
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0022")
        reset_engine()
        assert not _exists(db, "unit_cards") and _pk(db) == ["version_id", "reading_id", "unit_no"]
        assert _forced(db, "document_units")
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _exists(db, "unit_cards") and _forced(db, "unit_cards")
    assert _pk(db) == ["version_id", "reading_id", "derivation_key", "unit_no"]


def _counts(office) -> dict[str, int]:
    tables = ("documents", "document_versions", "document_units", "conversations", "version_dates")
    with tenant_tx(office.system) as conn:
        return {t: conn.execute(text(f"SELECT count(*) FROM {t}")).scalar_one() for t in tables}


def test_the_downgrade_deletes_no_unit_it_relabels_the_older_derivations_and_an_upgrade_again_keeps_every_row(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    doc, ver = make_document(a, a.default_group_id, title="שומה סינתטית")
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("INSERT INTO conversations (office_id, user_id, title, engine) VALUES"
                          " (app_office(), :u, 'שיחה', 'rag')"), {"u": a.admin_id})
        # one version, one reading, two derivations of units 1 and 2; the newer is units-v5
        _unit(conn, doc, ver, 1, "units-v4|-", "ישנה א", created="now() - interval '2 days'")
        _unit(conn, doc, ver, 2, "units-v4|-", "ישנה ב", created="now() - interval '2 days'")
        _unit(conn, doc, ver, 1, "units-v5|-", "חדשה א")
        _unit(conn, doc, ver, 2, "units-v5|-", "חדשה ב")
        _unit(conn, doc, ver, 3, "units-v5|-", "חדשה ג")   # a number the older derivation lacks: nothing collides
        _card(conn, doc, ver)
        _job(conn, ver, "unit_cards", "unit_cards:1")
        _job(conn, ver, "process", "process:1")
    before = _counts(a)
    cfg = alembic_config()
    try:
        command.downgrade(cfg, "0022")
        reset_engine()
        assert not _exists(db, "unit_cards") and _forced(db, "document_units") and _forced(db, "jobs")
        assert _pk(db) == ["version_id", "reading_id", "unit_no"]
        assert _counts(a) == before   # nothing was deleted
        with tenant_tx(a.system) as conn:
            readable = [(r.unit_no, r.title) for r in conn.execute(text(
                "SELECT unit_no, title FROM document_units WHERE reading_id = 'reading-1' ORDER BY unit_no"))]
            relabelled = sorted(r.title for r in conn.execute(text(
                "SELECT title FROM document_units WHERE reading_id <> 'reading-1'")))
            jobs = [r.kind for r in conn.execute(text("SELECT kind FROM jobs"))]
        assert readable == [(1, "חדשה א"), (2, "חדשה ב"), (3, "חדשה ג")]   # the newest row of each number is the one read
        assert relabelled == ["ישנה א", "ישנה ב"]
        assert jobs == ["process"]   # only the job kind the old check cannot hold is removed
    finally:
        command.upgrade(cfg, "head")
        reset_engine()
    assert _pk(db) == ["version_id", "reading_id", "derivation_key", "unit_no"] and _forced(db, "unit_cards")
    after = _counts(a)
    assert after == before
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM unit_cards")).scalar_one() == 0   # the cards come again with the backfill
