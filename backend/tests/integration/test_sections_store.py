"""The stored structure of a standard (round 10 U4: R3, R21–R24; KTD9, KTD11).

Sections and the text digest are derived from the stored blocks, never from the file: ``ensure`` writes them once per
version, reading and ``SECTIONS_VERSION`` (single-flight, nothing written twice), replaces the rows of an older reading
and deletes nothing else, and serves only professional kinds and only to those who may see the document. The
ingestion of a standard stores them after publishing and a failing derivation is recorded, never raised. The readers
the answer tools use (``section_of``, ``section_text``, ``sections_of``) read them. Synthetic data only."""

from __future__ import annotations

import threading
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app import professional, worker
from app.db import tenant_tx
from app.platform import pipeline
from app.professional import sections as S
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.integration.test_documents_kind import post_kind
from tests.support.professional_files import MARK, professional_file

pytestmark = pytest.mark.db

D1 = Path(__file__).resolve().parents[1] / "fixtures" / "D1_synthetic_harozim_digital.pdf"


def rows():
    """A synthetic standard (tuples as ``professional_file`` takes them): sections 1, 2, 2.1 with a limit and a
    reference, 2.2, a definitions section and an appendix."""
    return [
        ("paragraph", None, [], f"{MARK} — תקן 98.1", 1),
        ("heading", "1", ["1. כללי"], "1. כללי", 1),
        ("paragraph", None, ["1. כללי"], "ראה סעיף 2.1 וגם תקן 99.", 1),
        ("heading", "2", ["2. דרישות"], "2. דרישות", 1),
        ("heading", "2.1", ["2. דרישות", "2.1 מדידה"], "2.1 מדידה", 1),
        ("paragraph", None, ["2. דרישות", "2.1 מדידה"], "חובה למדוד את כל השטח הבנוי.", 1),
        ("paragraph", None, ["2. דרישות", "2.1 מדידה"], "למעט שטחים משותפים.", 2),
        ("heading", "2.2", ["2. דרישות", "2.2 תיעוד"], "2.2 תיעוד", 2),
        ("paragraph", None, ["2. דרישות", "2.2 תיעוד"], "מומלץ לצרף תרשים.", 2),
        ("heading", None, ["הגדרות"], "הגדרות", 2),
        ("paragraph", None, ["הגדרות"], "שטח נטו – השטח הפנימי הסינתטי.", 2),
        ("paragraph", None, ["הגדרות"], "מעטפת – הקו החיצוני הסינתטי.", 3),
        ("heading", None, ["נספח א'"], "נספח א'", 3),
        ("paragraph", None, ["נספח א'"], "טבלה סינתטית.", 3),
    ]


@pytest.fixture
def office(db):
    return make_office(db, "משרד א", "admin-a@example.test")


def standard(office, kind="standard", **kw):
    return professional_file(office, office.default_group_id, kw.pop("blocks", rows()), source_kind=kind, **kw)


def stored_rows(office, version, sql="SELECT * FROM document_sections WHERE version_id = :v ORDER BY section_no"):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(sql), {"v": UUID(version)}).all()


def ingestion(office, version) -> dict:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT ingestion FROM document_versions WHERE id = :v"),
                            {"v": UUID(version)}).scalar_one()


# --- ensure -----------------------------------------------------------------------------------------------------

def test_ensure_derives_and_stores_the_sections_with_the_digest_from_the_stored_blocks(office):
    doc, ver = standard(office)
    got = S.ensure(office.system, ver)
    assert [s.label for s in got if s.label] == ["1", "2", "2.1", "2.2", "שטח נטו", "מעטפת", "נספח א'"]
    stored = stored_rows(office, ver)
    assert len(stored) == len(got) == 9
    assert {(r.reading_id, r.sections_version, str(r.document_id)) for r in stored} == {
        ("reading-professional-1", S.SECTIONS_VERSION, doc)}
    by_label = {r.label: r for r in stored}
    assert by_label["2.1"].limits == [{"block": 6, "sentence": "למעט שטחים משותפים.", "marker": "למעט",
                                       "provision_block": 5}]
    assert by_label["2.1"].parent_no == by_label["2"].section_no
    assert by_label["1"].refs[0]["target"] == by_label["2.1"].section_no
    assert by_label["1"].refs[1] == {"type": "standard", "standard": "99", "target": None, "block": 2,
                                     "text": "תקן 99"}
    with tenant_tx(office.system) as conn:
        assert S.stored(conn, ver) == got  # what is stored reads back as what was derived
        digest = S.derive_version(conn, ver)[1]
    ing = ingestion(office, ver)
    assert ing["text_digest"] == digest and len(digest) == 64
    assert ing["sections_derived"] == {"reading_id": "reading-professional-1", "version": S.SECTIONS_VERSION,
                                       "count": 9}
    assert ing["reading_id"] == "reading-professional-1"  # the rest of the report is untouched


def test_a_second_ensure_on_an_unchanged_reading_derives_and_writes_nothing(office, monkeypatch):
    _, ver = standard(office)
    first = S.ensure(office.system, ver)
    before = stored_rows(office, ver, "SELECT section_no, created_at, xmin::text FROM document_sections"
                                      " WHERE version_id = :v ORDER BY section_no")
    version_before = stored_rows(office, ver, "SELECT xmin::text FROM document_versions WHERE id = :v")

    def boom(*a, **k):
        raise AssertionError("an unchanged reading must not be derived again")

    monkeypatch.setattr(S, "derive", boom)
    monkeypatch.setattr(S, "_write", boom)
    assert S.ensure(office.system, ver) == first
    assert stored_rows(office, ver, "SELECT section_no, created_at, xmin::text FROM document_sections"
                                    " WHERE version_id = :v ORDER BY section_no") == before
    assert stored_rows(office, ver, "SELECT xmin::text FROM document_versions WHERE id = :v") == version_before


def test_a_new_reading_derives_anew_and_replaces_only_the_older_readings_rows(office):
    doc, ver = standard(office)
    old = S.ensure(office.system, ver)
    # another version of the same document keeps its rows: only this version's older reading goes
    other_doc, other_ver = standard(office, title="פרק ב")
    S.ensure(office.system, other_ver)
    with tenant_tx(office.system) as conn:  # a reindex: new reading id, new text
        conn.execute(text("UPDATE document_versions SET ingestion = ingestion || '{\"reading_id\": \"reading-2\"}'"
                          " WHERE id = :v"), {"v": ver})
        conn.execute(text("UPDATE document_blocks SET text = 'למעט חריג חדש.' WHERE version_id = :v"
                          " AND block_index = 6"), {"v": ver})
    new = S.ensure(office.system, ver)
    assert len(new) == len(old)
    assert {r.reading_id for r in stored_rows(office, ver)} == {"reading-2"}
    assert [lim["sentence"] for s in new for lim in s.limits if s.label == "2.1"] == ["למעט חריג חדש."]
    assert len(stored_rows(office, other_ver)) == 9  # untouched
    assert ingestion(office, ver)["sections_derived"]["reading_id"] == "reading-2"


def test_rows_of_an_older_sections_version_are_replaced_not_duplicated(office, monkeypatch):
    _, ver = standard(office)
    S.ensure(office.system, ver)
    monkeypatch.setattr(S, "SECTIONS_VERSION", "sections-v2")
    S.ensure(office.system, ver)
    got = stored_rows(office, ver)
    assert len(got) == 9 and {r.sections_version for r in got} == {"sections-v2"}


def test_ensure_serves_professional_kinds_only(office):
    _, appraisal = standard(office, kind="appraisal")
    assert S.ensure(office.system, appraisal) == ()
    assert stored_rows(office, appraisal) == []
    assert "text_digest" not in ingestion(office, appraisal)
    _, professional = standard(office, kind="professional", title="חוזר")
    assert len(S.ensure(office.system, professional)) == 9
    assert S.ensure(office.system, UUID(int=1)) == ()  # no such version


def test_a_document_changed_to_standard_after_ingestion_gets_its_sections_on_first_use(office):
    doc, ver = standard(office, kind="appraisal")
    assert S.ensure(office.system, ver) == ()
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE documents SET source_kind = 'standard' WHERE id = :d"), {"d": UUID(doc)})
    assert len(S.ensure(office.system, ver)) == 9
    assert len(stored_rows(office, ver)) == 9


def test_a_reading_without_text_derives_no_section_and_is_not_derived_again(office, monkeypatch):
    _, ver = standard(office, blocks=[("paragraph", None, [], MARK, 1)])
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_blocks SET text = ''"))
    assert S.ensure(office.system, ver) == ()
    assert ingestion(office, ver)["sections_derived"]["count"] == 0

    def boom(*a, **k):
        raise AssertionError("derived again")

    monkeypatch.setattr(S, "derive_version", boom)
    assert S.ensure(office.system, ver) == ()


def test_four_callers_at_once_derive_once_and_store_one_set(office, monkeypatch):
    _, ver = standard(office)
    calls = []
    real = S.derive_version
    gate = threading.Barrier(4)

    def counting(conn, version_id):
        calls.append(1)
        return real(conn, version_id)

    monkeypatch.setattr(S, "derive_version", counting)
    results, errors = [], []

    def run():
        try:
            gate.wait()
            results.append(S.ensure(office.system, ver))
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors, errors
    assert len(calls) == 1 and len(results) == 4 and all(r == results[0] for r in results)
    assert len(stored_rows(office, ver)) == 9
    assert not S._LOCKS  # the locks of finished builds are forgotten


def test_a_user_outside_the_documents_group_neither_derives_nor_reads_sections(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    other_group = make_group(a, "קבוצה אחרת")
    outsider = make_user(a, "outsider@example.test", [other_group])
    member = make_user(a, "member@example.test", [a.default_group_id])
    _, ver = standard(a)
    assert S.ensure(a.ctx("employee", outsider), ver) == ()
    assert stored_rows(a, ver) == []
    assert len(S.ensure(a.ctx("employee", member), ver)) == 9  # a member derives it, under the policy
    with tenant_tx(a.ctx("employee", outsider)) as conn:
        assert conn.execute(text("SELECT count(*) FROM document_sections")).scalar_one() == 0
        assert S.sections_of(conn, ver) == ()
    b = make_office(db, "משרד ב", "admin-b@example.test")
    with tenant_tx(b.system) as conn:
        assert S.sections_of(conn, ver) == () and conn.execute(
            text("SELECT count(*) FROM document_sections")).scalar_one() == 0


def test_the_derivation_reads_stored_blocks_only(office, monkeypatch):
    """No file: the version's storage key points at nothing, and storage is never asked."""
    _, ver = standard(office)
    monkeypatch.setattr("app.platform.storage.get_storage",
                        lambda: (_ for _ in ()).throw(AssertionError("the file must not be read")))
    assert len(S.ensure(office.system, ver)) == 9


# --- the readers ------------------------------------------------------------------------------------------------

def test_section_of_returns_the_innermost_section_holding_a_block(office):
    _, ver = standard(office)
    with tenant_tx(office.system) as conn:
        assert S.section_of(conn, ver, 6).label == "2.1"      # the limit's block
        assert S.section_of(conn, ver, 3).label == "2"        # a block of the chapter itself
        assert S.section_of(conn, ver, 10).label == "שטח נטו"  # a definition
        assert S.section_of(conn, ver, 13).label == "נספח א'"
        assert S.section_of(conn, ver, 0).role == "preamble"
        assert S.section_of(conn, ver, 99) is None
        assert [s.section_no for s in S.sections_of(conn, ver)] == list(range(1, 10))


def test_section_text_reads_the_whole_section_with_subsections_limits_and_resolved_references(office):
    _, ver = standard(office)
    with tenant_tx(office.system) as conn:
        chapter = S.section_text(conn, ver, "2")
        assert [s.label for s in chapter.subsections] == ["2.1", "2.2"]
        assert "חובה למדוד את כל השטח הבנוי." in chapter.text and "מומלץ לצרף תרשים." in chapter.text
        # the chapter's own sentences carry no limit; the subsection's limit comes with the whole section
        assert [lim["sentence"] for lim in chapter.limits] == ["למעט שטחים משותפים."]
        sub = S.section_text(conn, ver, "2.1")
        assert sub.text.splitlines() == ["2.1 מדידה", "חובה למדוד את כל השטח הבנוי.", "למעט שטחים משותפים."]
        assert (sub.section.page_first, sub.section.page_last) == (1, 2)
        first = S.section_text(conn, ver, "1")
        assert [(r["status"], r.get("target_label")) for r in first.refs] == [("resolved", "2.1"), ("external", None)]
        assert first.refs[1]["standard"] == "99" and first.refs[1]["target"] is None
        assert S.section_text(conn, ver, "נספח א'").section.role == "appendix"
        assert S.section_text(conn, ver, "נספח א").section.role == "appendix"   # the quote mark is optional
        assert S.section_text(conn, ver, S.sections_of(conn, ver)[3].section_no).section.label == "2.1"
        assert S.section_text(conn, ver, "7.7") is None and S.section_text(conn, ver, 99) is None


def test_an_unresolved_internal_reference_is_reported_as_such(office):
    blocks = [("paragraph", None, [], MARK, 1), ("heading", "1", ["1. כללי"], "1. כללי", 1),
              ("paragraph", None, ["1. כללי"], "ראה סעיף 8.", 1)]
    _, ver = standard(office, blocks=blocks)
    with tenant_tx(office.system) as conn:
        assert [r["status"] for r in S.section_text(conn, ver, "1").refs] == ["unresolved"]


# --- the same text, counted once ------------------------------------------------------------------------------------

def test_the_same_text_stored_as_two_files_has_one_digest_and_edited_text_another(office):
    _, a = standard(office, title="עותק א", sha="a" * 64)
    _, b = standard(office, title="עותק ב", sha="b" * 64)
    edited = rows()
    edited[5] = ("paragraph", None, ["2. דרישות", "2.1 מדידה"], "חובה למדוד חלק מהשטח הבנוי.", 1)
    _, c = standard(office, title="עותק ג", blocks=edited, sha="c" * 64)
    for v in (a, b, c):
        S.ensure(office.system, v)
    digests = [ingestion(office, v)["text_digest"] for v in (a, b, c)]
    assert digests[0] == digests[1] != digests[2]


# --- ingestion --------------------------------------------------------------------------------------------------

@pytest.fixture
def ingesting(db, client, monkeypatch):
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    return a, client


def upload(office, client, tag, kind):
    data = D1.read_bytes() + f"\n%{tag}\n".encode()
    res = post_kind(client, f"{tag}.pdf", data, office.default_group_id, kind).json()["results"][0]
    assert res["status"] == "accepted", res
    while worker.run_one("test-worker"):
        pass
    return res["document_id"], res["version_id"]


def test_ingesting_a_standard_stores_its_sections_and_digest_after_publishing(ingesting):
    office, client = ingesting
    _, ver = upload(office, client, "std-ingest", "standard")
    with tenant_tx(office.system) as conn:
        status = conn.execute(text("SELECT status FROM document_versions WHERE id = :v"), {"v": UUID(ver)}).scalar_one()
    assert status == "ready"
    assert len(stored_rows(office, ver)) >= 1
    ing = ingestion(office, ver)
    assert len(ing["text_digest"]) == 64 and ing["sections_derived"]["count"] >= 1
    assert "professional_error" not in ing


def test_ingesting_an_appraisal_stores_no_sections(ingesting):
    office, client = ingesting
    _, ver = upload(office, client, "apr-ingest", None)
    assert stored_rows(office, ver) == []
    assert "text_digest" not in ingestion(office, ver)


def test_a_failing_derivation_is_recorded_and_never_fails_the_publish(ingesting, monkeypatch):
    office, client = ingesting

    def boom(*a, **k):
        raise RuntimeError("synthetic derivation failure")

    with monkeypatch.context() as m:
        m.setattr(S, "derive", boom)
        doc, ver = upload(office, client, "std-fail", "professional")
    with tenant_tx(office.system) as conn:
        v = conn.execute(text("SELECT status, is_current FROM document_versions WHERE id = :v"),
                         {"v": UUID(ver)}).one()
    assert (v.status, v.is_current) == ("ready", True)
    assert stored_rows(office, ver) == []
    err = ingestion(office, ver)["professional_error"]
    assert err["stage"] == "sections" and err["error"] == "RuntimeError"
    # the next derivation (here the hook again) stores the sections and clears the record
    professional.after_publish(office.system, UUID(ver))
    assert len(stored_rows(office, ver)) >= 1 and "professional_error" not in ingestion(office, ver)


def test_reprocessing_a_standard_replaces_its_sections_with_the_new_readings(ingesting):
    office, client = ingesting
    _, ver = upload(office, client, "std-reindex", "standard")
    old = {r.reading_id for r in stored_rows(office, ver)}
    assert pipeline.reindex_version(office.office_id, UUID(ver)) == "reindexed"
    new = {r.reading_id for r in stored_rows(office, ver)}
    assert len(old) == len(new) == 1 and old != new
