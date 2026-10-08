"""Reading a processed document again (reindex): its reading is replaced, people's work is kept, and nothing
built on the old reading is served again. A reprocess that cannot replace the reading (a regression, or a transient
failure after the job's bounded attempts) ends as ``kept_previous`` with its reason, and the document stays available
without an admin (U12, KTD9)."""

from __future__ import annotations

import hashlib
import json

import pytest
from sqlalchemy import text

from app import worker
from app.db import tenant_tx
from app.extraction.images import VisionUnavailable
from app.platform import pipeline
from app.platform.jobs import enqueue_reindex
from app.platform.search import hybrid_search
from app.platform.storage import get_storage, storage_key
from tests.conftest import login
from tests.factories import make_office
from tests.integration.test_reprocess_gate import CORRECTIONS, LOST_120, NEW, OLD, reading
from tests.unit.test_docx_blocks import FIXTURE

pytestmark = pytest.mark.db


@pytest.fixture
def office(db, client):
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    groups = client.get("/api/admin/groups").json()["groups"]
    r = client.post("/api/documents", data={"group_id": groups[0]["id"]},
                    files={"files": (FIXTURE.name, FIXTURE.read_bytes(),
                                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document")})
    assert r.status_code == 200, r.text
    a.version = r.json()["results"][0]["version_id"]
    a.document = r.json()["results"][0]["document_id"]
    while worker.run_one("test-worker"):
        pass
    return a


def scalar(office, sql, **p):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(sql), p).scalar_one()


def test_reindex_replaces_the_reading_and_keeps_reviews(office):
    v = office.version
    assert scalar(office, "SELECT status FROM document_versions WHERE id = :v", v=v) in ("ready", "needs_review")
    assert scalar(office, "SELECT count(*) FROM document_blocks WHERE version_id = :v", v=v) > 0
    with tenant_tx(office.system) as conn:
        attr = conn.execute(text(
            "INSERT INTO attribute_definitions (office_id, key, label_he, value_type, source) VALUES (app_office(),"
            " 'k', 'תכונה', 'numeric', 'extracted') RETURNING id")).scalar_one()
        for status in ("verified", "needs_review"):
            conn.execute(text(
                "INSERT INTO facts (office_id, document_id, version_id, attribute_id, value_numeric, quote, source_path,"
                " extraction_version, status) VALUES (app_office(), :d, :v, :a, 1, 'q', CAST(:p AS jsonb), 'x7', :s)"),
                {"d": office.document, "v": v, "a": attr, "p": json.dumps({}), "s": status})
        conn.execute(text("INSERT INTO answer_cache (office_id, cache_key, payload) VALUES (app_office(), 'k', '{}')"))
        conn.execute(text("UPDATE document_blocks SET text = 'קריאה ישנה' WHERE version_id = :v"), {"v": v})
    before = scalar(office, "SELECT version FROM office_data_versions")
    status_before = scalar(office, "SELECT status FROM document_versions WHERE id = :v", v=v)

    with tenant_tx(office.system) as conn:
        assert enqueue_reindex(conn, v, pipeline.INGESTION_VERSION)
        assert not enqueue_reindex(conn, v, pipeline.INGESTION_VERSION)  # already queued: not twice
    while worker.run_one("test-worker"):
        pass

    assert scalar(office, "SELECT count(*) FROM document_blocks WHERE version_id = :v AND text = 'קריאה ישנה'",
                  v=v) == 0
    assert scalar(office, "SELECT ingestion->>'ingestion_version' FROM document_versions WHERE id = :v",
                  v=v) == pipeline.INGESTION_VERSION
    assert scalar(office, "SELECT status FROM document_versions WHERE id = :v", v=v) == status_before
    assert scalar(office, "SELECT count(*) FROM facts WHERE status = 'verified'") == 1  # a person's review stays
    assert scalar(office, "SELECT count(*) FROM facts WHERE status = 'needs_review'") == 0  # old reading's guess goes
    assert scalar(office, "SELECT count(*) FROM answer_cache") == 0
    assert scalar(office, "SELECT version FROM office_data_versions") > before
    assert scalar(office, "SELECT count(*) FROM chunks WHERE version_id = :v AND embedding IS NULL", v=v) == 0


def test_admin_reprocess_queues_only_outdated_versions_unless_all(office, client):
    assert client.post("/api/admin/reprocess", json={}).json()["queued"] == 0
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE document_versions SET ingestion = jsonb_set(ingestion, '{ingestion_version}',"
                          " '\"old\"') WHERE id = :v"), {"v": office.version})
    assert client.post("/api/admin/reprocess", json={}).json()["queued"] == 1


# --- PDFs: the block reader's provenance survives reindexing and cloning --------------------------------------

PICTURES_PDF = FIXTURE.parents[1] / "blocks" / "B2_synthetic_pictures.pdf"
_PROVENANCE = ("SELECT block_index, kind, page, section_path, status, bbox, method, reader_version, content_hash,"
               " original_text FROM document_blocks WHERE version_id = :v ORDER BY block_index")


def _upload_pictures_pdf(db, client):
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    groups = client.get("/api/admin/groups").json()["groups"]
    r = client.post("/api/documents", data={"group_id": groups[0]["id"]},
                    files={"files": (PICTURES_PDF.name, PICTURES_PDF.read_bytes(), "application/pdf")})
    assert r.status_code == 200, r.text
    a.version = r.json()["results"][0]["version_id"]
    a.document = r.json()["results"][0]["document_id"]
    while worker.run_one("test-worker"):
        pass
    return a


@pytest.fixture
def pdf_office(db, client, monkeypatch):
    """The pictures PDF read with neither OCR nor a vision model: its pictures stay unread, whatever tesseract the
    machine has (CI installs Hebrew OCR, a developer host may not)."""
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    return _upload_pictures_pdf(db, client)


@pytest.fixture
def pdf_office_ocr(db, client, monkeypatch):
    """The same PDF with OCR available and no model: OCR finds no confident words in the synthetic pictures."""
    from app.extraction import images

    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: True)
    monkeypatch.setattr(images, "_ocr_words", lambda gray, lang: [])
    return _upload_pictures_pdf(db, client)


def _blocks(office, v):
    with tenant_tx(office.system) as conn:
        return [tuple(r) for r in conn.execute(text(_PROVENANCE), {"v": v}).all()]


def test_a_pdf_is_stored_as_blocks_with_their_provenance(pdf_office, client):
    v = pdf_office.version
    blocks = _blocks(pdf_office, v)
    assert [b[1] for b in blocks] == ["image", "heading", "paragraph", "image", "paragraph", "image", "heading",
                                      "paragraph"]
    assert all(b[2] in (1, 2) and len(b[5]) == 4 and b[7] == pipeline.PDF_INGESTION_VERSION for b in blocks)
    pictures = [b for b in blocks if b[1] == "image"]
    assert all(b[4] == "unread" and b[6] == "none" and b[8] for b in pictures)
    ingestion = scalar(pdf_office, "SELECT ingestion FROM document_versions WHERE id = :v", v=v)
    assert ingestion["ingestion_version"] == pipeline.PDF_INGESTION_VERSION
    assert ingestion["blocks"] == {"image": 3, "heading": 2, "paragraph": 3}
    assert ingestion["images"] == {"unread": 3} and ingestion["partial"] is True
    assert scalar(pdf_office, "SELECT count(*) FROM chunks WHERE version_id = :v AND (block_start IS NULL"
                              " OR page_list IS NULL)", v=v) == 0
    body = client.get(f"/api/documents/{pdf_office.document}/versions/{v}/blocks").json()
    first = body["blocks"][0]
    assert first["kind"] == "image" and first["bbox"] == list(blocks[0][5]) and first["content_hash"] == blocks[0][8]
    assert first["method"] == "none" and first["reader_version"] == pipeline.PDF_INGESTION_VERSION


def test_with_ocr_and_no_model_pictures_without_words_hold_no_text(pdf_office_ocr):
    v = pdf_office_ocr.version
    pictures = [b for b in _blocks(pdf_office_ocr, v) if b[1] == "image"]
    assert len(pictures) == 3 and all(b[4] == "no_text" and b[6] == "ocr" for b in pictures)
    ingestion = scalar(pdf_office_ocr, "SELECT ingestion FROM document_versions WHERE id = :v", v=v)
    assert ingestion["images"] == {"no_text": 3} and ingestion["partial"] is False


def test_reindex_and_clone_keep_the_pdf_provenance(pdf_office, client):
    v = pdf_office.version
    before = _blocks(pdf_office, v)
    with tenant_tx(pdf_office.system) as conn:  # read by the older reader: the admin reprocess selects it
        conn.execute(text("UPDATE document_versions SET ingestion = jsonb_set(ingestion, '{ingestion_version}',"
                          " '\"docx-blocks-v3\"') WHERE id = :v"), {"v": v})
    assert client.post("/api/admin/reprocess", json={}).json()["queued"] == 1
    while worker.run_one("test-worker"):
        pass
    assert _blocks(pdf_office, v) == before
    assert scalar(pdf_office, "SELECT ingestion->>'ingestion_version' FROM document_versions WHERE id = :v",
                  v=v) == pipeline.PDF_INGESTION_VERSION
    assert client.post("/api/admin/reprocess", json={}).json()["queued"] == 0

    with tenant_tx(pdf_office.system) as conn:
        doc = conn.execute(text("SELECT document_id FROM document_versions WHERE id = :v"), {"v": v}).scalar_one()
        copy = conn.execute(text(
            "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
            " size_bytes, storage_key, status, cloned_from_version_id) SELECT office_id, document_id, 2, sha256,"
            " filename, mime_type, size_bytes, storage_key, 'processing', id FROM document_versions WHERE id = :v"
            " RETURNING id"), {"v": v}).scalar_one()
        pipeline.clone_outputs(conn, pipeline.VersionInfo(copy, doc, "k", "application/pdf", v))
    assert _blocks(pdf_office, copy) == before


# --- a reprocess that keeps the current reading (U12, KTD9, AE8) ------------------------------------------------
# A scripted reader (``tests.integration.test_reprocess_gate.reading``): no PDF is parsed and no model is called.


@pytest.fixture
def gated(db, client, monkeypatch):
    return make_gated(db, client, monkeypatch)


def make_gated(db, client, monkeypatch):
    """An office with one PDF version read by the scripted reader; ``gated.reader["next"]`` is what the next reading
    returns or raises, and ``gated.reader["calls"]`` counts the readings."""
    a = make_office(db, "משרד א", "admin-a@example.test")
    login(client, "admin-a@example.test")
    a.reader = {"next": reading(OLD), "calls": 0}

    def scripted(data, mime_type, deadline, vision, readings=None):
        a.reader["calls"] += 1
        r = a.reader["next"]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr(pipeline, "_extract", scripted)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: None)
    data = b"%PDF-1.4 synthetic kept-previous"
    sha = hashlib.sha256(data).hexdigest()
    key = storage_key(a.office_id, sha)
    get_storage().put(key, data)
    with tenant_tx(a.ctx()) as conn:
        a.document = conn.execute(text("INSERT INTO documents (office_id, group_id, title) VALUES (app_office(), :g,"
                                       " 'שומה סינתטית לקריאה חוזרת') RETURNING id"),
                                  {"g": a.default_group_id}).scalar_one()
        a.version = conn.execute(text(
            "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
            " size_bytes, storage_key, uploaded_by) VALUES (app_office(), :d, 1, :s, 'synthetic.pdf',"
            " 'application/pdf', :b, :k, :u) RETURNING id"),
            {"d": a.document, "s": sha, "b": len(data), "k": key, "u": a.admin_id}).scalar_one()
    pipeline.process_version(a.office_id, a.version)
    return a


def _reprocess(office, accept: bool = False) -> None:
    with tenant_tx(office.system) as conn:
        enqueue_reindex(conn, office.version, pipeline.PDF_INGESTION_VERSION, accept_regression=accept)
    while worker.run_one("test-worker"):
        pass


def _job(office):
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT status, attempts, max_attempts, last_error FROM jobs"
                                 " WHERE payload->>'mode' = 'reindex'")).one()


def _ingestion(office) -> dict:
    return scalar(office, "SELECT ingestion FROM document_versions WHERE id = :v", v=office.version)


def _reading_rows(office) -> tuple:
    with tenant_tx(office.system) as conn:
        return (conn.execute(text("SELECT block_index, text FROM document_blocks WHERE version_id = :v"
                                  " ORDER BY block_index"), {"v": office.version}).all(),
                conn.execute(text("SELECT chunk_index, text FROM chunks WHERE version_id = :v ORDER BY chunk_index"),
                             {"v": office.version}).all())


def test_a_reprocess_that_loses_numbers_keeps_the_previous_reading_without_an_admin(gated, client):
    """AE8: the job ends ``kept_previous`` (not failed) with the reason; the previous reading stays current, search
    still finds its passages, the documents screen and the admin jobs list say why, and no admin action is needed."""
    before, reading_id = _reading_rows(gated), _ingestion(gated)["reading_id"]
    status_before = scalar(gated, "SELECT status FROM document_versions WHERE id = :v", v=gated.version)
    gated.reader["next"] = reading(LOST_120, CORRECTIONS)
    _reprocess(gated)

    j = _job(gated)
    assert j.status == "kept_previous" and "120" in j.last_error
    assert gated.reader["calls"] == 2  # one reading at upload, one reprocess: permanent, not read again
    assert _reading_rows(gated) == before
    ing = _ingestion(gated)
    assert ing["reading_id"] == reading_id
    assert "120" in ing["reprocess_kept"]["reason"] and ing["reprocess_kept"]["attempts"] == 1
    assert scalar(gated, "SELECT status FROM document_versions WHERE id = :v", v=gated.version) == status_before
    with tenant_tx(gated.ctx()) as conn:
        hits = hybrid_search(conn, "השטח הבנוי 120", 5)
    assert hits and any("120" in h["text"] and str(h["version_id"]) == str(gated.version) for h in hits)

    doc = client.get(f"/api/documents/{gated.document}").json()
    kept = (doc.get("latest_version") or doc["versions"][0])["reading"]["kept_previous"]
    assert "120" in kept["reason"]
    body = client.get("/api/admin/jobs").json()
    assert any(r["status"] == "kept_previous" and r["kind"] == "process:reindex" for r in body["jobs"])
    assert [(k["version_id"], k["can_accept"]) for k in body["kept_previous"]] == [(str(gated.version), True)]


def test_an_admin_accept_after_kept_previous_requeues_the_job_and_applies_the_held_reading(gated, client):
    gated.reader["next"] = reading(LOST_120, CORRECTIONS)
    _reprocess(gated)
    assert _job(gated).status == "kept_previous"

    r = client.post("/api/admin/reprocess", json={"accept_regression": True}).json()
    assert r["queued"] == 1 and r["versions"] == [str(gated.version)]  # the kept job is queued again
    while worker.run_one("test-worker"):
        pass
    assert _job(gated).status == "done"
    ing = _ingestion(gated)
    assert "reprocess_kept" not in ing and "reprocess_regression" not in ing
    assert ing["accepted_regression"]["pages"] == [{"page": 1, "missing_numbers": ["120"]}]
    assert client.get("/api/admin/jobs").json()["kept_previous"] == []


def _drain_with_retries(office) -> None:
    """Run the reindex job through every attempt it has, without waiting for its backoff."""
    for _ in range(20):
        while worker.run_one("test-worker"):
            pass
        if _job(office).status != "queued":
            return
        with tenant_tx(office.system) as conn:
            conn.execute(text("UPDATE jobs SET run_after = now() WHERE status = 'queued'"))


def test_a_transient_vision_failure_retries_within_its_attempts_then_keeps_the_previous_reading(gated, client):
    before, reading_id = _reading_rows(gated), _ingestion(gated)["reading_id"]
    gated.reader["next"] = VisionUnavailable("timeout")
    with tenant_tx(gated.system) as conn:
        enqueue_reindex(conn, gated.version, pipeline.PDF_INGESTION_VERSION)
    _drain_with_retries(gated)

    j = _job(gated)
    assert j.status == "kept_previous" and j.attempts == j.max_attempts and "timeout" in j.last_error
    assert gated.reader["calls"] == 1 + j.max_attempts  # bounded: the job's own attempts, no more
    assert _reading_rows(gated) == before and _ingestion(gated)["reading_id"] == reading_id
    kept = _ingestion(gated)["reprocess_kept"]
    assert kept["attempts"] == j.max_attempts and "timeout" in kept["reason"]
    assert "reprocess_regression" not in _ingestion(gated)  # nothing for an admin to accept
    assert client.get("/api/admin/jobs").json()["kept_previous"][0]["can_accept"] is False


def test_a_transient_failure_that_clears_on_a_later_attempt_swaps_the_reading(gated):
    gated.reader["next"] = VisionUnavailable("rate_limited")
    with tenant_tx(gated.system) as conn:
        enqueue_reindex(conn, gated.version, pipeline.PDF_INGESTION_VERSION)
    while worker.run_one("test-worker"):
        pass
    assert _job(gated).status == "queued"  # attempts remain: retried, not kept yet
    assert "reprocess_kept" not in _ingestion(gated)
    gated.reader["next"] = reading(NEW, CORRECTIONS)
    _drain_with_retries(gated)
    assert _job(gated).status == "done" and "reprocess_kept" not in _ingestion(gated)
