"""Reading a processed document again (reindex): its reading is replaced, people's work is kept, and nothing
built on the old reading is served again."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app import worker
from app.db import tenant_tx
from app.platform import pipeline
from app.platform.jobs import enqueue_reindex
from tests.conftest import login
from tests.factories import make_office
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
