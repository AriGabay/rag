"""The lazily derived dates record (round 11 U6b: R5, R6, R10; KTD3, KTD4; AE2).

`version_dates` holds the valuation dates an appraisal version states, derived on first use from its stored blocks with
the label vocabulary and kept per `DATES_VERSION`; a version with none gets a marker row so it is not derived again.
The owning unit is found at read time from the current `document_units` ranges. Files are built from blocks and stored
units (`tests/support/unit_files`), so the ranges are exact. Synthetic data only: every title, date and figure is
invented."""

from __future__ import annotations

import json
from datetime import date
from uuid import UUID

import pytest
from sqlalchemy import event, text

from app.chat import dates as D
from app.chat import units
from app.db import tenant_tx
from tests.factories import make_document, make_group, make_office, make_user
from tests.support.professional_files import professional_file
from tests.support.unit_files import reread, unit_file

pytestmark = pytest.mark.db

ANCHOR = date(2026, 10, 14)
HEADER = "שומה סינתטית — נתונים בדויים"


@pytest.fixture
def office(db):
    return make_office(db, "משרד א", "admin-a@example.test")


def appraisal(office, units_, sha: str = "d" * 64, group=None):
    return unit_file(office, group or office.default_group_id, "שומה סינתטית", units_, sha=sha)


def rows(office, version, sql="SELECT * FROM version_dates WHERE version_id = :v ORDER BY block_index, iso"):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(sql), {"v": UUID(version)}).all()


def read(office, *versions, cache=None, ctx=None):
    with tenant_tx(ctx or office.system) as conn:
        got = D.valuation_dates(conn, list(versions), cache=cache)
    return got


def inserts(conn) -> list[str]:
    """Collects the statements that write `version_dates` on this connection."""
    seen: list[str] = []

    @event.listens_for(conn, "before_cursor_execute")
    def _watch(_c, _cur, statement, *_a):
        if statement.lstrip().upper().startswith("INSERT INTO VERSION_DATES"):
            seen.append(statement)

    return seen


def mark(office, version, first_block: int, section_path: list[str]) -> None:
    """The stored blocks from ``first_block`` on sit under a section (as the reader stores a heading's path)."""
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET section_path = :p WHERE version_id = :v AND block_index >= :b"),
                     {"p": section_path, "v": UUID(version), "b": first_block})


def as_table(office, doc, version, block_index: int, headers: list[str], *rows: list[str]) -> None:
    """A stored block becomes a table block with the structure the reader stores (`extracted_tables`)."""
    structure = {"headers": headers, "caption": None, "title": [], "notes": [], "section": None,
                 "block_index": block_index, "rows": [{"cells": r} for r in rows]}
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET table_index = 0 WHERE version_id = :v AND block_index = :b"),
                     {"v": UUID(version), "b": block_index})
        conn.execute(text("INSERT INTO extracted_tables (office_id, document_id, version_id, table_index, page_start,"
                          " page_end, structure) VALUES (app_office(), :d, :v, 0, 1, 1, CAST(:s AS jsonb))"),
                     {"d": UUID(str(doc)), "v": UUID(version), "s": json.dumps(structure, ensure_ascii=False)})


def single(office, blocks, **kw):
    """A one-unit appraisal made of the given blocks."""
    return appraisal(office, [{"title": "שומה סינתטית", "blocks": blocks}], **kw)


# --- derivation ---------------------------------------------------------------------------------------------------

def test_ae2_the_valuation_date_is_stored_and_the_report_date_never_is(office):
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024", "תאריך עריכת השומה: 22/04/2025", "שווי הנכס 2,450,000 ₪"])
    got = read(office, ver)[UUID(ver)]
    assert [(d.iso, d.precision, d.block_index, d.unit_no, d.status) for d in got] == [
        ("2024-04-15", "day", 1, 1, "stated")]
    stored = rows(office, ver)
    assert [(r.iso, r.precision, r.written, r.block_index, r.status, r.dates_version) for r in stored] == [
        ("2024-04-15", "day", "15/04/2024", 1, "stated", D.DATES_VERSION)]
    assert "2025-04-22" not in {r.iso for r in stored}


def test_ae2_an_adopted_prose_value_takes_the_units_valuation_date(office):
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024", "תאריך עריכת השומה: 22/04/2025", "שווי הנכס 2,450,000 ₪"])
    with tenant_tx(office.system) as conn:
        uv = D.unit_valuation(conn, ver, 3)
    assert (uv.status, uv.date.iso, uv.unit_no) == ("stated", "2024-04-15", 1)
    got = D.resolve_as_of("שווי הנכס 2,450,000 ₪", stance="adopted", unit_valuation=uv,
                          anchor=ANCHOR)
    assert (got["date"], got["provenance"], got["locator"]) == ("2024-04-15", "unit_valuation", "block 1")


def test_an_adopted_conclusion_in_a_summary_table_cell_of_the_same_unit_takes_it_too(office):
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024", "|שווי הנכס | 2,450,000 ₪"])
    with tenant_tx(office.system) as conn:
        uv = D.unit_valuation(conn, ver, 2)
    got = D.resolve_as_of("שווי הנכס | 2,450,000 ₪", stance="adopted", unit_valuation=uv,
                          anchor=ANCHOR)
    assert (got["date"], got["provenance"]) == ("2024-04-15", "unit_valuation")


def test_a_header_line_value_with_an_unknown_stance_takes_the_units_valuation_date(office):
    """Round 11 U15 A: "שווי הנכס: 2,410,000 ₪" under the unit's header is registered with the stance unknown."""
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024", "שווי הנכס: 2,410,000 ₪"])
    with tenant_tx(office.system) as conn:
        uv = D.unit_valuation(conn, ver, 2)
    got = D.resolve_as_of("שווי הנכס: 2,410,000 ₪", stance="unknown", role="other", unit_valuation=uv, anchor=ANCHOR)
    assert (got["date"], got["provenance"]) == ("2024-04-15", "unit_valuation")


def test_a_party_claim_with_no_date_stays_unknown_even_in_a_unit_with_a_valuation_date(office):
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024", "לטענת המשיבה שווי הנכס 2,000,000 ₪"])
    with tenant_tx(office.system) as conn:
        uv = D.unit_valuation(conn, ver, 2)
    assert D.resolve_as_of("לטענת המשיבה שווי הנכס 2,000,000 ₪", stance="claim",
                           unit_valuation=uv, anchor=ANCHOR) is None


def test_a_multi_unit_file_gives_each_unit_its_own_date_and_a_block_outside_every_unit_none(office):
    _, ver = appraisal(office, [
        {"title": "שומה סינתטית א", "blocks": [HEADER, "המועד הקובע: 15/04/2024", "שווי 2,450,000 ₪"]},
        {"title": "שומה סינתטית ב", "blocks": [HEADER, "המועד הקובע ליום 01/02/2025", "שווי 3,100,000 ₪"]},
        {"title": "נספח סינתטי", "kind": "appendix", "blocks": ["טבלת עסקאות", "|תל אביב | 1,900,000"]},
    ])
    with tenant_tx(office.system) as conn:
        dates = D.valuation_dates(conn, [ver])[UUID(ver)]
        assert [(d.iso, d.unit_no) for d in dates] == [("2024-04-15", 1), ("2025-02-01", 2)]
        assert [D.unit_valuation(conn, ver, b).date.iso for b in (0, 2)] == ["2024-04-15"] * 2
        assert [D.unit_valuation(conn, ver, b).date.iso for b in (3, 5)] == ["2025-02-01"] * 2
        appendix = D.unit_valuation(conn, ver, 7)
        assert (appendix.status, appendix.date, appendix.unit_no) == ("none", None, 3)  # a unit with no stated date
        conn.execute(text("UPDATE document_units SET block_last = block_last - 1 WHERE version_id = :v AND unit_no = 3"),
                     {"v": UUID(ver)})
        outside = D.unit_valuation(conn, ver, 7)
    assert (outside.status, outside.date, outside.unit_no) == ("none", None, None)


def test_two_differing_valuation_dates_in_one_unit_are_a_stored_conflict_and_unknown_for_ranking(office):
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024", "שווי 2,450,000 ₪", "המועד הקובע: 01/06/2024"])
    read(office, ver)
    stored = rows(office, ver)
    assert [(r.iso, r.status) for r in stored] == [("2024-04-15", "conflict"), ("2024-06-01", "conflict")]
    with tenant_tx(office.system) as conn:
        uv = D.unit_valuation(conn, ver, 2)
    assert (uv.status, uv.date) == ("conflict", None) and {d.iso for d in uv.dates} == {"2024-04-15", "2024-06-01"}
    assert D.resolve_as_of("שווי 2,450,000 ₪", stance="adopted", unit_valuation=uv,
                           anchor=ANCHOR) is None


def test_the_same_date_stated_twice_in_a_unit_is_no_conflict(office):
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024", "שווי 2,450,000 ₪", "למועד הקובע ליום 15.4.2024"])
    read(office, ver)
    assert {r.status for r in rows(office, ver)} == {"stated"}


def test_a_version_with_no_valuation_date_gets_one_marker_row_and_reads_empty(office):
    _, ver = single(office, [HEADER, "תאריך עריכת השומה: 22/04/2025", "שווי 2,450,000 ₪"])
    assert read(office, ver)[UUID(ver)] == ()
    marker = rows(office, ver)
    assert [(r.block_index, r.iso, r.precision, r.written, r.dates_version) for r in marker] == [
        (-1, None, None, None, D.DATES_VERSION)]
    with tenant_tx(office.system) as conn:
        assert D.unit_valuation(conn, ver, 1).status == "none"


def test_a_professional_kind_version_derives_nothing_and_stores_nothing(office):
    _, std = professional_file(office, office.default_group_id, [
        ("heading", None, [], "1. כללי", 1), ("paragraph", None, ["1. כללי"], "המועד הקובע: 15/04/2024", 1)])
    assert read(office, std).get(UUID(std), ()) == ()
    assert rows(office, std) == []


def test_a_kind_change_to_professional_keeps_the_rows_unread_and_back_reads_them_again(office):
    doc, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024"])
    assert len(read(office, ver)[UUID(ver)]) == 1
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE documents SET source_kind = 'professional' WHERE id = :d"), {"d": doc})
    assert read(office, ver).get(UUID(ver), ()) == ()
    assert len(rows(office, ver)) == 1  # kept, never deleted
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE documents SET source_kind = 'appraisal' WHERE id = :d"), {"d": doc})
    assert len(read(office, ver)[UUID(ver)]) == 1 and len(rows(office, ver)) == 1


# --- permissions ---------------------------------------------------------------------------------------------------

def test_a_user_outside_the_documents_group_reads_no_rows_and_builds_none(office, db):
    closed = make_group(office, "קבוצה סגורה")
    outsider = make_user(office, "outside@example.test", [office.default_group_id])
    insider = make_user(office, "inside@example.test", [office.default_group_id, closed])
    other = make_office(db, "משרד ב", "admin-b@example.test")
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024"], group=closed)
    out_ctx, in_ctx = office.ctx("employee", outsider), office.ctx("employee", insider)
    assert read(office, ver, ctx=out_ctx).get(UUID(ver), ()) == ()
    assert read(office, ver, ctx=other.ctx()).get(UUID(ver), ()) == ()
    assert rows(office, ver) == []  # nothing was built for either, not even a marker
    assert len(read(office, ver, ctx=in_ctx)[UUID(ver)]) == 1
    assert len(rows(office, ver)) == 1
    with tenant_tx(out_ctx) as conn:  # the rows exist now; the outsider still reads none of them
        assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 0
        assert D.valuation_dates(conn, [ver]).get(UUID(ver), ()) == ()
        assert D.unit_valuation(conn, ver, 1).status == "none"
    with tenant_tx(other.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM version_dates")).scalar_one() == 0


# --- units are found at read time --------------------------------------------------------------------------------

def test_a_re_derived_unit_range_assigns_the_date_to_the_unit_that_holds_its_block_now(office):
    _, ver = appraisal(office, [
        {"title": "שומה סינתטית א", "blocks": [HEADER, "המועד הקובע: 15/04/2024", "שווי 2,450,000 ₪"]},
        {"title": "שומה סינתטית ב", "blocks": [HEADER, "שווי 3,100,000 ₪"]},
    ])
    with tenant_tx(office.system) as conn:
        assert [(d.iso, d.unit_no) for d in D.valuation_dates(conn, [ver])[UUID(ver)]] == [("2024-04-15", 1)]
    before = rows(office, ver)
    # the units are read again: the file is now cut so that block 1 belongs to a new first unit of one block only
    reread(office, ver, "reading-units-2", [
        {"title": "חזית", "blocks": (0, 0), "pages": (1, 1)},
        {"title": "שומה סינתטית א", "blocks": (1, 2), "pages": (1, 1)},
        {"title": "שומה סינתטית ב", "blocks": (3, 4), "pages": (2, 2)},
    ])
    with tenant_tx(office.system) as conn:
        assert [(d.iso, d.unit_no) for d in D.valuation_dates(conn, [ver])[UUID(ver)]] == [("2024-04-15", 2)]
        assert D.unit_valuation(conn, ver, 2).unit_no == 2
        assert D.unit_valuation(conn, ver, 4).status == "none"
    assert [(r.iso, r.block_index) for r in rows(office, ver)] == [(r.iso, r.block_index) for r in before]


def test_a_version_with_no_units_is_one_scope_the_document(office, monkeypatch):
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024", "שווי 2,450,000 ₪"])
    monkeypatch.setattr(units, "ensure", lambda conn, version_id: ())
    with tenant_tx(office.system) as conn:
        got = D.valuation_dates(conn, [ver])[UUID(ver)]
        uv = D.unit_valuation(conn, ver, 2)
    assert [(d.iso, d.unit_no) for d in got] == [("2024-04-15", 0)]
    assert (uv.status, uv.date.iso, uv.unit_no) == ("stated", "2024-04-15", 0)


# --- caching ------------------------------------------------------------------------------------------------------

def test_the_second_call_inserts_nothing_and_the_per_turn_cache_issues_no_query(office):
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024"])
    with tenant_tx(office.system) as conn:
        first = inserts(conn)
        cache: dict = {}
        D.valuation_dates(conn, [ver], cache=cache)
        assert len(first) == 1
        seen: list[str] = []
        event.listen(conn, "before_cursor_execute", lambda _c, _cur, statement, *_a: seen.append(statement))
        D.valuation_dates(conn, [ver], cache=cache)
        assert D.unit_valuation(conn, ver, 1, cache=cache).status == "stated"
        assert seen == []
    with tenant_tx(office.system) as conn:  # a later transaction finds the stored rows and derives nothing
        second = inserts(conn)
        assert len(D.valuation_dates(conn, [ver])[UUID(ver)]) == 1
        assert second == []
    assert len(rows(office, ver)) == 1


def test_a_marker_row_stops_the_derivation_of_a_version_with_no_dates(office):
    _, ver = single(office, [HEADER, "שווי 2,450,000 ₪"])
    read(office, ver)
    with tenant_tx(office.system) as conn:
        later = inserts(conn)
        assert D.valuation_dates(conn, [ver])[UUID(ver)] == ()
        assert later == []
    assert len(rows(office, ver)) == 1


def test_a_new_dates_version_derives_again_and_a_current_one_never_does(office, monkeypatch):
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024"])
    read(office, ver)
    monkeypatch.setattr(D, "DATES_VERSION", "dates-v5")
    assert len(read(office, ver)[UUID(ver)]) == 1
    assert sorted(r.dates_version for r in rows(office, ver)) == ["dates-v4", "dates-v5"]  # the old rows are kept
    with tenant_tx(office.system) as conn:
        again = inserts(conn)
        D.valuation_dates(conn, [ver])
        assert again == []


def test_the_current_dates_version_is_v4(office):
    assert D.DATES_VERSION == "dates-v4"


def old_row(office, doc, version, dates_version: str, iso: str | None, block: int = 1) -> None:
    with tenant_tx(office.system) as conn:
        conn.execute(text(
            "INSERT INTO version_dates (office_id, document_id, version_id, dates_version, block_index, iso, precision,"
            " written, status) VALUES (app_office(), :d, :v, :dv, :b, :i, :p, :w, 'stated')"),
            {"d": UUID(str(doc)), "v": UUID(version), "dv": dates_version, "b": block, "i": iso,
             "p": "day" if iso else None, "w": iso})


def test_a_version_with_only_dates_v1_rows_derives_the_current_dates_on_first_read_and_keeps_the_v1_rows_unread(office):
    """Round 12 U3 (R21): the stored v1 rows are a derived cache of an older reading; they are neither deleted nor read."""
    doc, ver = single(office, [HEADER, "התאריך הקובע לשומה: 28/02/2024", "שווי 2,450,000 ₪"])
    old_row(office, doc, ver, "dates-v1", None, D.MARKER_BLOCK)   # what v1 stored: it found nothing in this file
    got = read(office, ver)[UUID(ver)]
    assert [(d.iso, d.block_index) for d in got] == [("2024-02-28", 1)]
    assert sorted((r.dates_version, r.iso) for r in rows(office, ver)) == [("dates-v1", None), ("dates-v4", "2024-02-28")]
    with tenant_tx(office.system) as conn:   # the next read is served from the v2 rows, with no new write
        later = inserts(conn)
        assert [d.iso for d in D.valuation_dates(conn, [ver])[UUID(ver)]] == ["2024-02-28"]
        assert later == []


def test_a_v1_row_is_never_read_as_a_date_of_the_current_version(office):
    doc, ver = single(office, [HEADER, "שווי 2,450,000 ₪"])
    old_row(office, doc, ver, "dates-v1", "2019-01-01")
    assert read(office, ver)[UUID(ver)] == ()
    assert sorted((r.dates_version, r.iso) for r in rows(office, ver)) == [("dates-v1", "2019-01-01"), ("dates-v4", None)]


def test_a_read_only_transaction_over_a_v1_only_version_still_gets_the_v2_dates_and_writes_nothing(office):
    doc, ver = single(office, [HEADER, "המועד הקובע: 31.12.23"])
    old_row(office, doc, ver, "dates-v1", None, D.MARKER_BLOCK)
    with tenant_tx(office.system) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        got = D.valuation_dates(conn, [ver])[UUID(ver)]
    assert [d.iso for d in got] == ["2023-12-31"]
    assert [(r.dates_version, r.iso) for r in rows(office, ver)] == [("dates-v1", None)]


# --- dates-v2 from the stored blocks: the section path and the table structure (U3, KTD11, KTD12; AE6) -------------

def test_ae6_a_prior_appraisals_label_under_a_history_section_never_becomes_the_units_date(office):
    _, ver = single(office, [
        HEADER, "המועד הקובע לשומה - 31.12.23", "3. שומות קודמות",
        "לתאריך הקובע 31.12.19 הוערך הנכס ב-1,800,000 ₪.", "בשומה קודמת, לתאריך הקובע 31.12.21, הוערך הנכס ב-2,000,000 ₪."])
    mark(office, ver, 2, ["3. שומות קודמות"])
    got = read(office, ver)[UUID(ver)]
    assert [(d.iso, d.block_index, d.status) for d in got] == [("2023-12-31", 1, "stated")]
    with tenant_tx(office.system) as conn:
        uv = D.unit_valuation(conn, ver, 3)
    assert (uv.status, uv.date.iso) == ("stated", "2023-12-31")   # no conflict with 2019 or 2021
    assert [(r.iso, r.status) for r in rows(office, ver)] == [("2023-12-31", "stated")]


def test_a_round_11_label_under_a_history_heading_is_not_stored(office):
    _, ver = single(office, [HEADER, "שומות מקרקעין קודמות", "המועד הקובע 30.6.22"])
    mark(office, ver, 1, ["שומות מקרקעין קודמות"])
    assert read(office, ver)[UUID(ver)] == ()
    assert [(r.block_index, r.iso) for r in rows(office, ver)] == [(D.MARKER_BLOCK, None)]


def test_a_one_row_table_header_label_is_read_from_the_stored_structure(office):
    doc, ver = single(office, [HEADER, "|מועד קובע | כתובת | סוג נכס\n7.11.2020 | הרימון 4 | דירה", "שווי 2,450,000 ₪"])
    as_table(office, doc, ver, 1, ["מועד קובע", "כתובת", "סוג נכס"], ["7.11.2020", "הרימון 4", "דירה"])
    got = read(office, ver)[UUID(ver)]
    assert [(d.iso, d.block_index, d.unit_no) for d in got] == [("2020-11-07", 1, 1)]


def test_a_multi_row_header_label_table_is_history_and_stores_no_unit_date(office):
    doc, ver = single(office, [
        HEADER, "המועד הקובע: 30/06/2025",
        "|מועד קובע | שווי (₪)\n31/12/2021 | 2,050,000\n31/12/2023 | 2,300,000"])
    as_table(office, doc, ver, 2, ["מועד קובע", "שווי (₪)"], ["31/12/2021", "2,050,000"], ["31/12/2023", "2,300,000"])
    assert [(d.iso, d.status) for d in read(office, ver)[UUID(ver)]] == [("2025-06-30", "stated")]
    _, only = single(office, [HEADER, "|מועד קובע | שווי (₪)\n31/12/2020 | 1,950,000\n31/12/2022 | 2,150,000"],
                     sha="e" * 64)
    doc2 = rows(office, only, "SELECT document_id FROM document_blocks WHERE version_id = :v LIMIT 1")[0].document_id
    as_table(office, doc2, only, 1, ["מועד קובע", "שווי (₪)"], ["31/12/2020", "1,950,000"], ["31/12/2022", "2,150,000"])
    assert read(office, only)[UUID(only)] == ()


def test_a_table_block_without_a_stored_structure_is_still_read_as_text(office):
    _, ver = single(office, [HEADER, "|מועד קובע | 15/04/2024"])
    assert [d.iso for d in read(office, ver)[UUID(ver)]] == ["2024-04-15"]


def test_a_transaction_that_cannot_write_still_gets_the_dates(office):
    _, ver = single(office, [HEADER, "המועד הקובע: 15/04/2024"])
    with tenant_tx(office.system) as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        got = D.valuation_dates(conn, [ver])[UUID(ver)]
    assert [d.iso for d in got] == ["2024-04-15"] and rows(office, ver) == []


def test_many_versions_are_read_in_one_call(office):
    _, v1 = single(office, [HEADER, "המועד הקובע: 15/04/2024"], sha="a" * 64)
    _, v2 = single(office, [HEADER, "המועד הקובע: 01/02/2025"], sha="b" * 64)
    _, v3 = single(office, [HEADER, "שווי 2,450,000 ₪"], sha="c" * 64)
    with tenant_tx(office.system) as conn:
        got = D.valuation_dates(conn, [v1, v2, v3, "00000000-0000-0000-0000-000000000000"])
    assert {k: [d.iso for d in v] for k, v in got.items()} == {
        UUID(v1): ["2024-04-15"], UUID(v2): ["2025-02-01"], UUID(v3): []}


def test_a_version_with_no_blocks_reads_empty_beside_one_with_a_date(office):
    _, v1 = single(office, [HEADER, "המועד הקובע: 15/04/2024"], sha="a" * 64)
    _, v2 = make_document(office, office.default_group_id, "מסמך ללא בלוקים", sha="e" * 64)
    with tenant_tx(office.system) as conn:
        got = D.valuation_dates(conn, [v1, v2])
    assert [d.iso for d in got[UUID(v1)]] == ["2024-04-15"] and got[UUID(str(v2))] == ()


# --- dates-v3: the primary section is stored and settles the unit at read time (U3b, KTD11) -----------------------

def primary_rows(office, ver):
    return [(r.block_index, r.iso, r.primary_section) for r in rows(office, ver)]


def test_a_date_from_the_valuation_section_is_stored_primary_and_decides_the_units_status(office):
    _, ver = single(office, [
        HEADER, "2. המועד הקובע לשומה: 01/07/2020", "16. נתונים נוספים",
        "הנכס הוערך על ידנו לצורך הדוחות הכספיים, למועד קובע 31.12.2022 בסך 2,000,000 ₪."])
    got = read(office, ver)[UUID(ver)]
    assert [(d.iso, d.primary, d.status) for d in got] == [("2020-07-01", True, "stated"), ("2022-12-31", False, "stated")]
    assert primary_rows(office, ver) == [(1, "2020-07-01", True), (3, "2022-12-31", False)]
    with tenant_tx(office.system) as conn:
        uv = D.unit_valuation(conn, ver, 3)
    assert (uv.status, uv.date.iso) == ("stated", "2020-07-01")
    assert [d.iso for d in uv.dates] == ["2020-07-01", "2022-12-31"]   # the reference is still listed


def test_the_primary_flag_survives_a_second_read_from_the_stored_rows(office):
    _, ver = single(office, [
        HEADER, "המועד הקובע להערכה זו - הינו 31.12.2023.", "הנכס נבדק למועד קובע 31.12.2022 בלבד."])
    read(office, ver)
    with tenant_tx(office.system) as conn:   # stored rows only: nothing is derived or written
        later = inserts(conn)
        got = D.valuation_dates(conn, [ver])[UUID(ver)]
        uv = D.unit_valuation(conn, ver, 1)
        assert later == []
    assert sorted((d.iso, d.primary, d.status) for d in got) == [("2022-12-31", False, "stated"),
                                                                  ("2023-12-31", True, "stated")]
    assert (uv.status, uv.date.iso) == ("stated", "2023-12-31")


def test_the_genuine_discrepancy_with_no_primary_date_stays_a_conflict(office):
    _, ver = single(office, [HEADER, "השווי נקבע למועד הקובע 31.12.2021.", "השווי מתייחס למועד הקובע 30.12.2021."])
    got = read(office, ver)[UUID(ver)]
    assert {d.status for d in got} == {"conflict"} and not any(d.primary for d in got)
    with tenant_tx(office.system) as conn:
        uv = D.unit_valuation(conn, ver, 1)
    assert (uv.status, uv.date) == ("conflict", None)


def test_primary_dates_that_disagree_are_a_conflict(office):
    _, ver = single(office, [HEADER, "2. המועד הקובע: 01/07/2020", "3. המועד הקובע: 02/07/2020"])
    assert {d.status for d in read(office, ver)[UUID(ver)]} == {"conflict"}


def test_a_heading_that_is_the_label_makes_the_date_under_it_primary(office):
    _, ver = single(office, [HEADER, "2. המועד הקובע", "31.5.2021.", "16. נתונים נוספים",
                             "הנכס הוערך לצורך הדוחות הכספיים למועד קובע 31.12.2022."])
    mark(office, ver, 1, ["2. המועד הקובע"])
    mark(office, ver, 3, ["16. נתונים נוספים"])
    got = read(office, ver)[UUID(ver)]
    assert [(d.iso, d.primary, d.status) for d in got] == [("2021-05-31", True, "stated"), ("2022-12-31", False, "stated")]


def test_a_comparison_a_reference_list_and_a_relational_label_state_no_date_in_a_stored_file(office):
    _, ver = single(office, [
        HEADER, "2. המועד הקובע לשומה: 31.12.2022",
        "השינוי בין השומה למועד הקובע 31.12.2022 לשומה למועד הקובע 31.12.2019 נובע משינוי בשוק.",
        "סימוכין: שומה מס' 1-98 מתאריך 23.06.2021 (מועד קובע 26.05.2021) שומה מס' 1.1-9 מתאריך 03.05.2022 "
        "(מועד קובע 29.03.2022)",
        "עסקאות בנכס הנישום הסמוכות למועד הקובע : 12.03.2022 מכירת דירה"])
    got = read(office, ver)[UUID(ver)]
    assert [(d.iso, d.status) for d in got] == [("2022-12-31", "stated")]


def test_an_empty_header_table_with_the_label_in_its_first_row_is_read_from_the_stored_structure(office):
    doc, ver = single(office, [HEADER, "|תוכנית | מועד קובע\n251-0479345 | 7.11.2020", "שווי 2,450,000 ₪"])
    as_table(office, doc, ver, 1, [], ["תוכנית", "מועד קובע"], ["251-0479345", "7.11.2020"])
    assert [(d.iso, d.block_index) for d in read(office, ver)[UUID(ver)]] == [("2020-11-07", 1)]


def test_a_stored_dates_v3_row_is_never_read_and_the_current_version_derives_again(office):
    """Round 12 U3b follow-up: v3 rows were derived without the restated-label and bullet readings."""
    doc, ver = single(office, [HEADER, "\uf0b7 המועד הקובע לשומה – 31.12.2023."])
    old_row(office, doc, ver, "dates-v3", "2019-01-01")
    got = read(office, ver)[UUID(ver)]
    assert [(d.iso, d.primary) for d in got] == [("2023-12-31", True)]
    assert sorted((r.dates_version, r.iso) for r in rows(office, ver)) == [("dates-v3", "2019-01-01"),
                                                                           (D.DATES_VERSION, "2023-12-31")]


def test_a_restated_lead_label_and_a_bulleted_label_are_stored_primary(office):
    _, ver = single(office, [
        HEADER, "2. המועד הקובע לשומה: המועד הקובע לשומה הינו מועד הביקור בנכס 01/07/2020.",
        "הערך נכון למועד הקובע – 30.12.2023 בלבד.", "8.1 עסקאות בנכס הנישום הסמוכות למועד הקובע : מכירה מיום 08.11.2018"])
    got = read(office, ver)[UUID(ver)]
    assert [(d.iso, d.primary, d.status) for d in got] == [("2020-07-01", True, "stated"), ("2023-12-30", False, "stated")]
    assert primary_rows(office, ver) == [(1, "2020-07-01", True), (2, "2023-12-30", False)]
