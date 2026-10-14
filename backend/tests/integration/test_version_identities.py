"""The lazily derived identification record (round 12 U5: R1, R2, R5, R13, R21; KTD1, KTD2, KTD3, KTD15).

`version_identities` holds the identification facts an appraisal version states, derived on first use from its stored
blocks and kept per `IDENTITY_VERSION`; a version with none gets a marker row so it is not derived again. The owning unit
is found at read time from the current `document_units` ranges, and `document_units.identifiers` stay as stored. Files
are built from blocks and stored units (`tests/support/unit_files`), so the ranges are exact. Synthetic data only: every
name, address and figure is invented."""

from __future__ import annotations

from uuid import UUID

import pytest
from sqlalchemy import event, text

from app.appraisal.publish import publish_version
from app.chat import contexts, units
from app.chat import identity as I  # noqa: E741
from app.db import tenant_tx
from app.platform import pipeline
from tests.factories import make_group, make_office, make_user
from tests.support.professional_files import professional_file
from tests.support.unit_files import reread, unit_file

pytestmark = pytest.mark.db

HEADER = "שומה סינתטית — נתונים בדויים"
IDENT = ["עיר: עיר-סינתטית", "כתובת הנכס: הדולב 7", "גוש: 8201 חלקה: 11"]


@pytest.fixture
def office(db):
    return make_office(db, "משרד א", "admin-a@example.test")


def addr(street: str, number: str) -> tuple[str, str]:
    """An address as the facts hold it: the street core, as `contexts.identifiers` cores it."""
    (core,) = contexts.identifiers(f"רחוב {street} {number}").addresses
    return core


def appraisal(office, units_, sha: str = "i" * 64, group=None):
    return unit_file(office, group or office.default_group_id, "שומה סינתטית", units_, sha=sha)


def single(office, blocks, **kw):
    return appraisal(office, [{"title": "שומה סינתטית", "blocks": blocks}], **kw)


def rows(office, version, sql="SELECT * FROM version_identities WHERE version_id = :v ORDER BY block_index, kind, written"):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(sql), {"v": UUID(version)}).all()


def read(office, *versions, cache=None, ctx=None):
    with tenant_tx(ctx or office.system) as conn:
        return I.identities(conn, list(versions), cache=cache)


def inserts(conn) -> list[str]:
    """Collects the statements that write `version_identities` on this connection."""
    seen: list[str] = []

    @event.listens_for(conn, "before_cursor_execute")
    def _watch(_c, _cur, statement, *_a):
        if statement.lstrip().upper().startswith("INSERT INTO VERSION_IDENTITIES"):
            seen.append(statement)

    return seen


# --- derivation and storage ----------------------------------------------------------------------------------------

def test_the_facts_of_a_version_are_derived_stored_and_read_as_the_units_identity(office):
    _, ver = single(office, [HEADER, *IDENT, "שווי הנכס: 2,500,000 ₪"])
    got = read(office, ver)[UUID(ver)]
    ident = got[1]
    assert ident.parcels == {("8201", "11", None)} and ident.addresses == {addr("הדולב", "7")}
    assert ident.cities == {"עיר-סינתטית"} and ident.link_city == "עיר-סינתטית" and ident.identified
    stored = rows(office, ver)
    assert {r.kind for r in stored} == {"city", "address", "parcel"}
    assert {r.identity_version for r in stored} == {I.IDENTITY_VERSION}
    assert all(r.section == "label" or r.section == "title" for r in stored)
    parcel = next(r for r in stored if r.kind == "parcel")
    assert (parcel.block, parcel.parcel, parcel.sub_parcel, parcel.block_index) == ("8201", "11", None, 3)


def test_a_version_naming_nothing_gets_one_marker_row_and_reads_a_unit_that_is_not_identified(office):
    _, ver = single(office, [HEADER, "שווי הנכס: 2,500,000 ₪"])
    got = read(office, ver)[UUID(ver)]
    assert set(got) == {1} and not got[1].identified and got[1].link_city is None
    marker = rows(office, ver)
    assert [(r.block_index, r.kind, r.identity_version) for r in marker] == [(-1, "marker", I.IDENTITY_VERSION)]
    with tenant_tx(office.system) as conn:
        later = inserts(conn)
        assert not I.identities(conn, [ver])[UUID(ver)][1].identified
        assert later == []
    assert len(rows(office, ver)) == 1


def test_the_second_call_inserts_nothing_and_the_per_turn_cache_issues_no_query(office):
    _, ver = single(office, [HEADER, *IDENT])
    with tenant_tx(office.system) as conn:
        first = inserts(conn)
        cache: dict = {}
        I.identities(conn, [ver], cache=cache)
        assert len(first) == 1   # one executemany for all the facts
        seen: list[str] = []
        event.listen(conn, "before_cursor_execute", lambda _c, _cur, statement, *_a: seen.append(statement))
        I.identities(conn, [ver], cache=cache)
        I.facts(conn, [ver], cache=cache)
        assert seen == []
    with tenant_tx(office.system) as conn:   # a later transaction finds the stored rows and derives nothing
        second = inserts(conn)
        assert I.identities(conn, [ver])[UUID(ver)][1].parcels == {("8201", "11", None)}
        assert second == []
    assert len(rows(office, ver)) == 3


def test_many_versions_are_read_in_one_call_and_an_unknown_one_is_left_out(office):
    _, a = single(office, [HEADER, *IDENT], sha="a" * 64)
    _, b = single(office, [HEADER, "כתובת הנכס: הדס 3", "גוש: 8401 חלקות: 24, 40"], sha="b" * 64)
    got = read(office, a, b, "00000000-0000-0000-0000-000000000000")
    assert set(got) == {UUID(a), UUID(b)}
    assert got[UUID(b)][1].parcels == {("8401", "24", None), ("8401", "40", None)}


def test_a_new_identity_version_derives_again_and_a_current_one_never_does(office, monkeypatch):
    _, ver = single(office, [HEADER, *IDENT])
    read(office, ver)
    monkeypatch.setattr(I, "IDENTITY_VERSION", "identity-v2")
    assert read(office, ver)[UUID(ver)][1].parcels == {("8201", "11", None)}
    assert sorted({r.identity_version for r in rows(office, ver)}) == ["identity-v1", "identity-v2"]   # the old rows stay
    with tenant_tx(office.system) as conn:
        again = inserts(conn)
        I.identities(conn, [ver])
        assert again == []


def test_a_professional_kind_version_derives_nothing_and_stores_nothing(office):
    _, std = professional_file(office, office.default_group_id, [
        ("heading", None, [], "1. כללי", 1), ("paragraph", None, ["1. כללי"], "גוש: 8201 חלקה: 11", 1)])
    assert read(office, std).get(UUID(std)) is None
    assert rows(office, std) == []


# --- permissions -------------------------------------------------------------------------------------------------------

def test_a_user_outside_the_documents_group_reads_no_rows_and_builds_none(office, db):
    closed = make_group(office, "קבוצה סגורה")
    outsider = make_user(office, "outside@example.test", [office.default_group_id])
    insider = make_user(office, "inside@example.test", [office.default_group_id, closed])
    other = make_office(db, "משרד ב", "admin-b@example.test")
    _, ver = single(office, [HEADER, *IDENT], group=closed)
    out_ctx, in_ctx = office.ctx("employee", outsider), office.ctx("employee", insider)
    assert read(office, ver, ctx=out_ctx).get(UUID(ver)) is None
    assert read(office, ver, ctx=other.ctx()).get(UUID(ver)) is None
    assert rows(office, ver) == []   # nothing was built for either, not even a marker
    assert read(office, ver, ctx=in_ctx)[UUID(ver)][1].identified
    assert len(rows(office, ver)) == 3
    with tenant_tx(out_ctx) as conn:   # the rows exist now; the outsider still reads none of them
        assert conn.execute(text("SELECT count(*) FROM version_identities")).scalar_one() == 0
        assert I.identities(conn, [ver]).get(UUID(ver)) is None
    with tenant_tx(other.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM version_identities")).scalar_one() == 0


# --- units are found at read time ------------------------------------------------------------------------------------------

def test_the_facts_of_two_units_go_to_their_own_unit_and_the_cover_and_comparables_give_none(office):
    _, ver = appraisal(office, [
        {"title": "פתיח", "kind": "front_matter", "blocks": ["כתובת: הברוש 4", "גוש: 8601 חלקה: 5"]},
        {"title": "שומה א", "blocks": [HEADER, "עיר: עיר-סינתטית", "כתובת הנכס: הברוש 4", "גוש: 8601 חלקה: 5"]},
        {"title": "שומה ב", "blocks": [HEADER, "עיר: עיר-סינתטית", "כתובת הנכס: הברוש 6", "גוש: 8601 חלקה: 6"]},
        {"title": "עסקאות השוואה", "kind": "comparables", "blocks": ["כתובת: הצפצפה 21", "גוש: 8902 חלקה: 3"]},
    ])
    got = read(office, ver)[UUID(ver)]
    assert set(got) == {2, 3}
    assert got[2].parcels == {("8601", "5", None)} and got[2].addresses == {addr("הברוש", "4")}
    assert got[3].parcels == {("8601", "6", None)} and got[3].addresses == {addr("הברוש", "6")}
    with tenant_tx(office.system) as conn:   # the facts of the cover and of the comparables part are stored, never assigned
        assigned = I.facts(conn, [ver])[UUID(ver)]
    assert {f.unit_no for f in assigned} == {2, 3}


def test_a_re_derived_unit_range_assigns_the_facts_to_the_unit_that_holds_their_block_now(office):
    _, ver = appraisal(office, [
        {"title": "שומה א", "blocks": [HEADER, *IDENT]},
        {"title": "שומה ב", "blocks": [HEADER, "כתובת הנכס: הדס 3", "גוש: 8401 חלקה: 24"]},
    ])
    assert read(office, ver)[UUID(ver)][1].parcels == {("8201", "11", None)}
    before = rows(office, ver)
    reread(office, ver, "reading-units-2", [
        {"title": "חזית", "blocks": (0, 0), "pages": (1, 1)},
        {"title": "שומה א", "blocks": (1, 3), "pages": (1, 1)},
        {"title": "שומה ב", "blocks": (4, 7), "pages": (2, 2)},
    ])
    got = read(office, ver)[UUID(ver)]
    assert got[2].parcels == {("8201", "11", None)} and got[3].parcels == {("8401", "24", None)}
    assert [(r.kind, r.block_index, r.written) for r in rows(office, ver)] == [
        (r.kind, r.block_index, r.written) for r in before]   # the rows were not rewritten


def test_a_fact_in_the_edge_of_a_probable_unit_belongs_to_neither_neighbour(office):
    _, ver = appraisal(office, [
        {"title": "שומה א", "blocks": [HEADER, "כתובת הנכס: הדס 3", "גוש: 8401 חלקה: 24"]},
        {"title": "שומה ב", "certainty": "probable", "blocks": [HEADER, "כתובת הנכס: הדס 5", "גוש: 8401 חלקה: 30"],
         "deep": ["פרטי הנכס", "גוש: 8401 חלקה: 31"]},
    ])
    got = read(office, ver)[UUID(ver)]
    assert got[1].parcels == frozenset()      # the last page of the unit before a probable cut
    assert got[2].parcels == {("8401", "31", None)}   # only the fact past the first page of the probable unit


def test_identification_only_under_the_heading_on_a_later_page_is_found_and_the_stored_unit_identifiers_stay(office):
    _, ver = appraisal(office, [{
        "title": "שומה סינתטית", "identifiers": [], "blocks": [HEADER, "סוג נכס: דירה", "שווי הנכס: 2,380,000 ₪"],
        "deep": ["3. זיהוי הנכס", "רחוב הלוטם 8, עיר-סינתטית-ב", "גוש: 8901 חלקה: 14", "4. סביבת הנכס",
                 "החלקה גובלת בחלקה 15 בגוש 8901."]}])
    with tenant_tx(office.system) as conn:
        before = conn.execute(text("SELECT unit_no, identifiers FROM document_units WHERE version_id = :v ORDER BY unit_no"),
                              {"v": UUID(ver)}).all()
    got = read(office, ver)[UUID(ver)][1]
    assert got.addresses == {addr("הלוטם", "8")} and got.parcels == {("8901", "14", None)}
    with tenant_tx(office.system) as conn:
        after = conn.execute(text("SELECT unit_no, identifiers FROM document_units WHERE version_id = :v ORDER BY unit_no"),
                             {"v": UUID(ver)}).all()
    assert [(r.unit_no, r.identifiers) for r in after] == [(r.unit_no, r.identifiers) for r in before]


def test_a_version_with_no_units_is_one_scope_the_document(office, monkeypatch):
    _, ver = single(office, [HEADER, *IDENT])
    monkeypatch.setattr(units, "ensure", lambda conn, version_id: ())
    monkeypatch.setattr(units, "stored_many", lambda conn, version_ids: {})
    got = read(office, ver)[UUID(ver)]
    assert set(got) == {I.DOCUMENT_SCOPE} and got[I.DOCUMENT_SCOPE].parcels == {("8201", "11", None)}


# --- the header city (KTD3) ------------------------------------------------------------------------------------------------

def test_a_single_unit_file_whose_body_names_no_city_takes_the_header_city(office, monkeypatch):
    _, ver = single(office, [HEADER, "כתובת הנכס: הדולב 7", "גוש: 8201 חלקה: 11"])
    monkeypatch.setattr(I, "report_header", lambda conn, version_id: {"city": "עיר-סינתטית"})
    ident = read(office, ver)[UUID(ver)][1]
    assert ident.cities == frozenset() and ident.header_city == "עיר-סינתטית" and ident.link_city == "עיר-סינתטית"


def test_a_file_of_several_subject_units_never_donates_a_header_city(office, monkeypatch):
    _, ver = appraisal(office, [
        {"title": "שומה א", "blocks": [HEADER, "כתובת הנכס: הברוש 4", "גוש: 8601 חלקה: 5"]},
        {"title": "שומה ב", "blocks": [HEADER, "כתובת הנכס: הברוש 6", "גוש: 8601 חלקה: 6"]}])
    monkeypatch.setattr(I, "report_header", lambda conn, version_id: {"city": "עיר-סינתטית"})
    got = read(office, ver)[UUID(ver)]
    assert all(i.header_city is None and i.link_city is None for i in got.values())


def test_the_header_is_not_asked_when_the_body_states_a_city(office, monkeypatch):
    _, ver = single(office, [HEADER, *IDENT])

    def boom(conn, version_id):
        raise AssertionError("the header must not be read")

    monkeypatch.setattr(I, "report_header", boom)
    assert read(office, ver)[UUID(ver)][1].link_city == "עיר-סינתטית"


# --- the refresh path (KTD15) ---------------------------------------------------------------------------------------------

def test_a_document_re_read_deletes_its_identity_rows_with_the_other_derived_text(office):
    _, ver = single(office, [HEADER, *IDENT])
    read(office, ver)
    assert len(rows(office, ver)) == 3
    assert "version_identities" in pipeline._DERIVED_TEXT_TABLES
    with tenant_tx(office.system) as conn:
        pipeline._delete_outputs(conn, UUID(ver))
    assert rows(office, ver) == []


def test_publishing_a_version_derives_its_identities_next_to_the_units(office):
    doc, ver = single(office, [HEADER, *IDENT])
    assert rows(office, ver) == []
    with tenant_tx(office.system) as conn:
        publish_version(conn, UUID(ver), UUID(doc), "test")
    assert {r.kind for r in rows(office, ver)} == {"city", "address", "parcel"}


def test_the_warm_up_script_derives_missing_rows_only_and_deletes_nothing(office):
    import importlib.util
    from pathlib import Path

    spec = importlib.util.spec_from_file_location(
        "derive_time_records", Path(__file__).resolve().parents[2] / "scripts" / "derive_time_records.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    _, a = single(office, [HEADER, *IDENT], sha="a" * 64)
    _, b = single(office, [HEADER, "כתובת הנכס: הדס 3", "גוש: 8401 חלקה: 24"], sha="b" * 64)
    read(office, a)    # one version already has its rows
    kept = rows(office, a)
    dry = script.warm_office(office.system, dry_run=True)
    assert dry["identities_derived"] == 1 and rows(office, b) == []   # a dry run writes nothing
    first = script.warm_office(office.system)
    assert first["identities_derived"] == 1 and first["versions"] == 2
    assert {r.kind for r in rows(office, b)} == {"address", "parcel"}
    assert [(r.kind, r.written) for r in rows(office, a)] == [(r.kind, r.written) for r in kept]
    again = script.warm_office(office.system)
    assert again["identities_derived"] == 0   # idempotent
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT count(*) FROM version_identities")).scalar_one() == len(kept) + len(rows(office, b))
        assert conn.execute(text("SELECT count(*) FROM documents")).scalar_one() == 2   # no document touched
