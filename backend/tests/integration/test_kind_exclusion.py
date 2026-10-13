"""Standards stay out of the appraisal surfaces and out of the appraisal stages of ingestion (round 10 U3: R1, R3, R9,
R28; KTD9, KTD12).

A standard's title holds trade words ("שומה", "גישת ההשוואה"); they must add nothing to the property vocabulary, to
appraisal counts, to unit locating or to measurement listings. Ingesting a professional file builds no units, records,
transactions or measurements, yet its version is published (current, ready, chunks and embeddings as before). A kind
changed after ingestion keeps its earlier derivations, which the filters exclude; a change back to ``appraisal`` runs
the skipped stages from the stored reading, never from the file.

The ingestion tests run the real worker over a synthetic PDF fixture (D1, uploaded under a kind); the others use
documents built from stored blocks or stored units. Generic passage search is not restricted here (U8 adds its pool
filter). Synthetic data only."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import text

from app import worker
from app.chat import contexts, entities
from app.chat import tools as T
from app.chat import units as unit_store
from app.db import tenant_tx
from app.measurements.extract import EXTRACTION_VERSION
from app.platform import pipeline
from app.platform.pipeline import run_measurements as real_run_measurements
from app.platform.search import documents_matching, units_matching, unread_documents
from app.platform.storage import get_storage
from tests.conftest import login
from tests.factories import make_document, make_office
from tests.integration.test_documents_kind import kind_of, post_kind
from tests.support.professional_files import MARK, professional_file
from tests.support.unit_files import unit_file

pytestmark = pytest.mark.db

D1 = Path(__file__).resolve().parents[1] / "fixtures" / "D1_synthetic_harozim_digital.pdf"
STANDARD_TITLE = "שומה גישת ההשוואה"  # trade words in a standard's title
APPRAISAL_TITLE = "שומה הרקפות 5"
SQL_COUNT = {
    "occurrences": "SELECT count(*) FROM occurrences", "transactions": "SELECT count(*) FROM transactions",
    "units": "SELECT count(*) FROM document_units", "chunks": "SELECT count(*) FROM chunks",
    "embedded": "SELECT count(*) FROM chunks WHERE embedding IS NOT NULL",
    "measure_jobs": "SELECT count(*) FROM jobs WHERE kind = 'extract_measurements'",
}


@pytest.fixture
def office(db, monkeypatch):
    a = make_office(db, "משרד א", "admin-a@example.test")
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    return a


def counts(office) -> dict:
    with tenant_tx(office.ctx()) as conn:
        return {name: conn.execute(text(sql)).scalar_one() for name, sql in SQL_COUNT.items()}


def data_version(office) -> int:
    with tenant_tx(office.ctx()) as conn:
        return conn.execute(text("SELECT version FROM office_data_versions")).scalar_one()


def run_all(n: int = 30) -> None:
    while n and worker.run_one("test-worker"):
        n -= 1


def ingest(client, office, tag: str, kind: str | None, name: str | None = None) -> tuple[str, str]:
    """D1 (an appraisal-shaped synthetic file) uploaded under ``kind`` and read by the real worker; ``tag`` makes its
    bytes differ."""
    data = D1.read_bytes() + f"\n%{tag}\n".encode()
    res = post_kind(client, name or f"{tag}.pdf", data, office.default_group_id, kind).json()["results"][0]
    assert res["status"] == "accepted", res
    run_all()
    return res["document_id"], res["version_id"]


def block_pair(office, kind: str):
    """An appraisal and a document of ``kind`` whose title holds trade words, built from stored blocks."""
    appraisal = professional_file(office, office.default_group_id, [("paragraph", None, [], "הנכס ברחוב הרקפות", 1)],
                                  title=APPRAISAL_TITLE, source_kind="appraisal")
    other = professional_file(
        office, office.default_group_id,
        [("heading", "1", ["1. כללי"], "1. כללי", 1),
         ("paragraph", None, ["1. כללי"], "יש לבצע את השומה בגישת ההשוואה כאשר קיימות עסקאות דומות.", 1)],
        title=STANDARD_TITLE, source_kind=kind)
    return appraisal, other


# --- the property vocabulary --------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["standard", "professional"])
def test_a_professional_titles_words_add_nothing_to_the_property_vocabulary(office, kind):
    (appraisal, _), (other, _) = block_pair(office, kind)
    titles = entities.titles_of(office.ctx())
    assert any(APPRAISAL_TITLE in t for t in titles)
    assert not any("גישת" in t for t in titles), titles
    # a follow-up naming only the standard's title words finds no property
    out = entities.lookup(office.ctx(), "גישת ההשוואה", set())
    assert out.kind == "not_found" and out.documents == []
    with tenant_tx(office.ctx()) as conn:
        assert documents_matching(conn, "גישת ההשוואה") == []
        assert units_matching(conn, "גישת ההשוואה").documents == []
        assert [d["document_id"] for d in documents_matching(conn, "הרקפות")] == [UUID(appraisal)]


def test_the_unit_names_and_the_first_message_hold_appraisals_only(office):
    doc, _ = unit_file(office, office.default_group_id, "שומה הגפן", [
        {"title": "מתחם הגפן א", "aliases": ["הגפן א"], "blocks": ["מתחם הגפן א, שומה"]},
        {"title": "מתחם הגפן ב", "aliases": ["הגפן ב"], "blocks": ["מתחם הגפן ב, שומה"]}])
    with tenant_tx(office.ctx()) as conn:
        assert len(list(conn.execute(text(entities._UNIT_NAMES_SQL)))) == 2
        assert "מתחם הגפן א" in entities._unit_names(conn)
    assert entities.first_message(office.ctx(), "מה השווי במתחם הגפן א?") is not None
    with tenant_tx(office.ctx()) as conn:  # the kind changes after the units were derived
        conn.execute(text("UPDATE documents SET source_kind = 'standard' WHERE id = :d"), {"d": doc})
    with tenant_tx(office.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM document_units WHERE document_id = :d"),
                            {"d": doc}).scalar_one() == 2  # the units stay stored (R1)
        assert list(conn.execute(text(entities._UNIT_NAMES_SQL))) == []  # and no longer name anything
        assert "מתחם הגפן א" not in entities._unit_names(conn) and "הגפן ב" not in entities._unit_names(conn)
        assert not any("הגפן" in t for t in entities._visible_titles(conn))
    assert entities.first_message(office.ctx(), "מה השווי במתחם הגפן א?") is None
    assert entities.live_unit(office.ctx(), {"document_id": doc, "unit_no": 1}).status == "unreadable"
    assert entities.units_of_documents(office.ctx(), [doc]) == []


def test_an_unread_standard_is_not_an_unread_appraisal(office):
    doc, ver = make_document(office, office.default_group_id, title=f"{MARK} — תקן שלא נקרא", sha="f" * 64,
                             source_kind="standard")
    _, _ = make_document(office, office.default_group_id, title="שומה שלא נקראה", sha="e" * 64)
    with tenant_tx(office.ctx()) as conn:
        conn.execute(text("UPDATE document_versions SET status = 'failed', is_current = false"))
        assert [d["title"] for d in unread_documents(conn)] == ["שומה שלא נקראה"]


def test_the_units_of_a_professional_version_are_not_built(office):
    (_, _), (std, std_ver) = block_pair(office, "standard")
    with tenant_tx(office.ctx()) as conn:
        assert unit_store.ensure(conn, std_ver) == ()
        assert unit_store.replace(conn, std_ver) == ()
        assert unit_store.rebuild_if_stale(conn, std_ver) is False
        contexts.of_version(conn, std_ver, "reading-professional-1")
        assert conn.execute(text("SELECT count(*) FROM document_units")).scalar_one() == 0


# --- the tools ---------------------------------------------------------------------------------------------

def test_find_documents_and_list_documents_count_appraisals_only(office):
    block_pair(office, "standard")
    professional_file(office, office.default_group_id, [("paragraph", None, [], "מסמך מקצועי", 1)],
                      title="שומה חוזר מקצועי", source_kind="professional")
    ws = T.Workspace(ctx=office.ctx())
    found = T.tool_find_documents(ws, "שומה")
    assert APPRAISAL_TITLE in found and "גישת" not in found and "חוזר" not in found
    listed = T.tool_list_documents(ws)
    assert APPRAISAL_TITLE in listed and 'סה"כ 1 מסמכים' in listed
    assert "גישת" not in listed and "חוזר" not in listed
    assert "תקן" not in T.tool_list_documents(T.Workspace(ctx=office.ctx()), "גישת")  # a title query finds none
    scope = ws.scope["documents"] if isinstance(ws.scope, dict) and "documents" in ws.scope else None
    assert scope is None or [d["title"] for d in scope] == [t for t in (d["title"] for d in scope) if APPRAISAL_TITLE in t]


def test_an_appraisal_titled_like_a_standard_is_still_found(office):
    professional_file(office, office.default_group_id, [("paragraph", None, [], "תוכן השומה", 1)],
                      title="תקן 19 - הרצל 7", source_kind="appraisal")
    professional_file(office, office.default_group_id, [("paragraph", None, [], "תוכן התקן", 1)],
                      title="תקן 19 גישת ההכנסות", source_kind="standard")
    ws = T.Workspace(ctx=office.ctx())
    found = T.tool_find_documents(ws, "תקן 19")
    assert "הרצל 7" in found and "גישת ההכנסות" not in found


def test_find_measurements_and_its_cached_listing_hold_appraisals_only(office):
    (appraisal, a_ver), (std, s_ver) = block_pair(office, "standard")
    quote = "השווי למ\"ר בנוי הוא 9,500 ₪"
    for doc, ver in ((appraisal, a_ver), (std, s_ver)):
        with tenant_tx(office.system) as conn:
            conn.execute(text(
                "INSERT INTO measurements (office_id, document_id, version_id, block_index, statement_key, metric,"
                " metric_kind, value, value_form, value_text, unit, period, vat, subject_role, value_role, quote,"
                " extraction_version, model, status, issues) VALUES (app_office(), :d, :v, 1, 'b1:x', 'שווי קרקע',"
                " 'value_per_area', 9500, 'exact', '9,500 ₪', 'ILS_per_sqm', 'none', 'unknown', 'general', 'other',"
                " :q, :e, 'test', 'auto_validated', '[]'::jsonb)"),
                {"d": doc, "v": ver, "q": quote, "e": EXTRACTION_VERSION})
            conn.execute(text(
                "INSERT INTO verified_values (office_id, document_id, version_id, reading_id, locator, value)"
                " VALUES (app_office(), :d, :v, 'reading-professional-1', CAST('{\"block\": 1}' AS jsonb),"
                " CAST(:val AS jsonb))"),
                {"d": doc, "v": ver, "val": json.dumps({
                    "verifier_version": T.VALUE_VERIFIER_VERSION,
                    "record": {"kind": "value_per_area", "quote": quote, "value_text": "9,500 ₪"}})})
    ws = T.Workspace(ctx=office.ctx())
    out = T.tool_find_measurements(ws, "שווי קרקע", ["value_per_area"])
    assert out.count(f'{APPRAISAL_TITLE}"') == 2 and "גישת" not in out  # the measurement and the cached value
    assert ws.coverage[-1]["documents"] == 1 and ws.coverage[-1]["total"] == 1


# --- ingestion ---------------------------------------------------------------------------------------------

def test_ingesting_a_standard_builds_no_appraisal_derivations_and_still_publishes_it(client, office, monkeypatch):
    login(client, "admin-a@example.test")
    cloud_on(monkeypatch)
    before = data_version(office)
    doc, ver = ingest(client, office, "std", "standard", "תקן בדיקה סינתטי — אינו תקן אמיתי.pdf")
    got = counts(office)
    assert (got["occurrences"], got["transactions"], got["units"], got["measure_jobs"]) == (0, 0, 0, 0), got
    assert got["chunks"] > 0 and got["embedded"] == got["chunks"]
    with tenant_tx(office.ctx()) as conn:
        v = conn.execute(text("SELECT status, is_current, records_total, processed_at FROM document_versions"
                              " WHERE id = :v"), {"v": ver}).one()
    assert v.status == "ready" and v.is_current and v.records_total == 0 and v.processed_at is not None
    assert data_version(office) > before  # published: the data version moved
    assert client.get(f"/api/documents/{doc}").json()["current_version"]["status"] == "ready"


def test_the_same_file_as_an_appraisal_does_build_them(client, office, monkeypatch):
    login(client, "admin-a@example.test")
    cloud_on(monkeypatch)
    ingest(client, office, "apr", None)
    got = counts(office)
    assert got["occurrences"] > 0 and got["transactions"] > 0 and got["units"] > 0 and got["measure_jobs"] == 1, got


def test_reprocessing_a_standard_builds_none_either(client, office, monkeypatch):
    login(client, "admin-a@example.test")
    doc, ver = ingest(client, office, "std2", "standard")
    cloud_on(monkeypatch)
    assert pipeline.reindex_version(office.office_id, UUID(ver)) == "reindexed"
    got = counts(office)
    assert (got["units"], got["occurrences"], got["measure_jobs"]) == (0, 0, 0), got
    assert got["embedded"] == got["chunks"] > 0


def test_measurements_are_not_extracted_for_a_standard_by_the_job_or_the_admin_queue(client, office, monkeypatch):
    login(client, "admin-a@example.test")
    doc, ver = ingest(client, office, "std3", "standard")
    apr_doc, apr_ver = ingest(client, office, "apr3", None)
    monkeypatch.setattr(pipeline, "run_measurements", real_run_measurements)
    cloud_on(monkeypatch)

    def boom(*a, **k):
        raise AssertionError("a standard's measurements must not be extracted")

    monkeypatch.setattr("app.measurements.extract.extract_version", boom)
    assert pipeline.run_measurements(office.office_id, UUID(ver)) == "skipped"
    r = client.post("/api/admin/measurements", json={"all": True})
    assert r.status_code == 200
    with tenant_tx(office.ctx()) as conn:
        queued = {str(v) for v in conn.execute(text("SELECT version_id FROM jobs WHERE kind = 'extract_measurements'"))
                  .scalars()}
    assert queued == {apr_ver}


def cloud_on(monkeypatch) -> None:
    """The office in cloud mode as the pipeline sees it (a vision reader exists, so measurement extraction is queued
    where the pipeline allows it). The synthetic file has no picture, so the reader is never called."""
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: object())


# --- a kind changed after ingestion ------------------------------------------------------------------------

def test_a_change_to_standard_keeps_the_derivations_and_the_surfaces_stop_reading_them(client, office):
    login(client, "admin-a@example.test")
    doc, ver = ingest(client, office, "chg1", None, name="שומה הגפן 12.pdf")
    before = counts(office)
    assert before["units"] > 0 and before["occurrences"] > 0
    assert any("הגפן" in t for t in entities.titles_of(office.ctx()))
    assert client.patch(f"/api/documents/{doc}", json={"source_kind": "standard"}).status_code == 200
    assert counts(office) == before  # nothing was deleted or derived (R1)
    assert not any("הגפן" in t for t in entities.titles_of(office.ctx()))
    assert "הגפן" not in T.tool_list_documents(T.Workspace(ctx=office.ctx()))
    with tenant_tx(office.ctx()) as conn:
        assert list(conn.execute(text(entities._UNIT_NAMES_SQL))) == []


def test_a_change_to_appraisal_runs_the_skipped_stages_from_the_stored_reading_without_the_file(
        client, office, monkeypatch):
    login(client, "admin-a@example.test")
    doc, ver = ingest(client, office, "chg2", "standard", name="שומה הגפן 12.pdf")
    assert counts(office)["units"] == 0 and counts(office)["occurrences"] == 0
    cloud_on(monkeypatch)
    # the file is not read: its storage fails for any read
    monkeypatch.setattr(get_storage(), "get", lambda key: (_ for _ in ()).throw(AssertionError("file read")),
                        raising=False)
    before = data_version(office)
    r = client.patch(f"/api/documents/{doc}", json={"source_kind": "appraisal"})
    assert r.status_code == 200, r.text
    got = counts(office)
    assert got["occurrences"] > 0 and got["transactions"] > 0 and got["units"] > 0 and got["measure_jobs"] == 1, got
    assert data_version(office) > before
    assert any("הגפן" in t for t in entities.titles_of(office.ctx()))
    assert "הגפן 12" in T.tool_list_documents(T.Workspace(ctx=office.ctx()))
    with tenant_tx(office.ctx()) as conn:  # the version's counts and status follow the records
        v = conn.execute(text("SELECT status, records_total FROM document_versions WHERE id = :v"), {"v": ver}).one()
    assert v.status in ("ready", "needs_review") and v.records_total == got["occurrences"]


def test_changing_the_kind_back_and_forth_deletes_and_duplicates_nothing(client, office):
    login(client, "admin-a@example.test")
    doc, ver = ingest(client, office, "chg3", None)
    settled = counts(office)
    for kind in ("professional", "appraisal", "standard", "appraisal"):
        assert client.patch(f"/api/documents/{doc}", json={"source_kind": kind}).status_code == 200
        assert {k: v for k, v in counts(office).items() if k != "measure_jobs"} == {
            k: v for k, v in settled.items() if k != "measure_jobs"}, kind
    assert kind_of(office, doc) == "appraisal"


def test_a_stage_that_fails_leaves_the_kind_as_it_was_with_a_reason(client, office, monkeypatch):
    login(client, "admin-a@example.test")
    doc, ver = ingest(client, office, "chg4", "standard")

    def boom(*a, **k):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr("app.appraisal.publish.derive_records", boom)
    r = client.patch(f"/api/documents/{doc}", json={"source_kind": "appraisal"})
    assert r.status_code == 409 and any("א" <= ch <= "ת" for ch in r.json()["detail"])
    assert kind_of(office, doc) == "standard"
    assert counts(office)["occurrences"] == 0 and counts(office)["units"] == 0


def test_a_change_to_appraisal_of_a_version_not_yet_read_runs_nothing_and_the_publish_follows_the_new_kind(
        client, office):
    login(client, "admin-a@example.test")
    data = D1.read_bytes() + b"\n%chg5\n"
    res = post_kind(client, "chg5.pdf", data, office.default_group_id, "standard").json()["results"][0]
    assert client.patch(f"/api/documents/{res['document_id']}", json={"source_kind": "appraisal"}).status_code == 200
    assert counts(office)["occurrences"] == 0  # nothing was read yet: nothing to run
    run_all()
    assert counts(office)["occurrences"] > 0  # the publish saw the appraisal
