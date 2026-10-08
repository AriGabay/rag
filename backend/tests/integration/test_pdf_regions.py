"""Uncovered PDF regions through the ingestion pipeline (U4): the office's ``image_readings`` reuse a picture's
reading across documents and re-ingestion but never across offices, a transient vision failure fails the job and
leaves the earlier reading in place, and a deterministic one leaves the region unread while the job completes.
Synthetic fixtures and a scripted vision reader only; no model is called."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import text

from app import worker
from app.db import tenant_tx
from app.platform import pipeline
from app.platform.jobs import enqueue_reindex
from app.platform.storage import get_storage, storage_key
from tests.factories import make_office
from tests.unit.test_regions import LOGO, LOGO_TEXT, LOWRES, R1, R2, TABLE, ScriptedVision

pytestmark = pytest.mark.db


@pytest.fixture
def offices(db, monkeypatch):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    readers: dict = {}
    monkeypatch.setattr("app.extraction.ocr.ocr_available", lambda languages: False)
    monkeypatch.setattr(pipeline, "vision_reader", lambda ctx: readers.get(ctx.office_id))
    monkeypatch.setattr(pipeline, "run_measurements", lambda office_id, version_id: "skipped")
    a.readers = b.readers = readers
    return a, b


def add_pdf(office, path) -> tuple:
    data = path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    key = storage_key(office.office_id, sha)
    get_storage().put(key, data)
    with tenant_tx(office.ctx()) as conn:
        doc = conn.execute(text("INSERT INTO documents (office_id, group_id, title) VALUES (app_office(), :g, :t)"
                                " RETURNING id"), {"g": office.default_group_id, "t": path.stem}).scalar_one()
        ver = conn.execute(text(
            "INSERT INTO document_versions (office_id, document_id, version_no, sha256, filename, mime_type,"
            " size_bytes, storage_key, uploaded_by) VALUES (app_office(), :d, 1, :s, :f, 'application/pdf', :b, :k,"
            " :u) RETURNING id"),
            {"d": doc, "s": sha, "f": path.name, "b": len(data), "k": key, "u": office.admin_id}).scalar_one()
    return doc, ver


def ingest(office, path, vision) -> object:
    office.readers[office.office_id] = vision
    _, ver = add_pdf(office, path)
    pipeline.process_version(office.office_id, ver)
    return ver


def rows(office, sql, **params):
    with tenant_tx(office.system) as conn:
        return conn.execute(text(sql), params).all()


def test_readings_are_reused_within_the_office_only(offices):
    a, b = offices
    first = ScriptedVision()
    ver = ingest(a, R1, first)
    assert first.count(LOGO) == 1 and first.count(TABLE) == 2  # without OCR the table's numbers stay unconfirmed
    # the logo on all ten pages is page furniture: one block, one chunk, counted in the ingestion report
    logos = rows(a, "SELECT page, status, text FROM document_blocks WHERE version_id = :v AND kind = 'image'"
                    " AND (bbox->>1)::float < 60 ORDER BY page", v=ver)
    assert [(r.page, r.status) for r in logos] == [(1, "read")] and LOGO_TEXT in logos[0].text
    assert len(rows(a, "SELECT 1 FROM chunks WHERE version_id = :v AND text LIKE :t", v=ver,
                    t=f"%{LOGO_TEXT}%")) == 1
    report = rows(a, "SELECT ingestion FROM document_versions WHERE id = :v", v=ver)[0].ingestion
    assert {(e["occurrences"], e["pages"], e["status"]) for e in report["repeated_images"]} == {
        (10, 10, "read"), (20, 10, "no_text")}
    pages = rows(a, "SELECT page_no, method FROM pages WHERE version_id = :v ORDER BY page_no", v=ver)
    assert [r.method for r in pages] == ["mixed"] + ["text_layer"] * 9
    table = rows(a, "SELECT structure FROM extracted_tables WHERE version_id = :v", v=ver)
    assert [t.structure["source"] for t in table] == ["vision"] and table[0].structure["rows"][0]["page"] == 1
    assert len(rows(a, "SELECT 1 FROM image_readings")) >= 2

    second = ScriptedVision()
    ingest(a, R2, second)
    assert second.count(LOGO) == 0 and second.count(LOWRES) >= 1  # the logo was read in the first document

    other = ScriptedVision()
    ingest(b, R2, other)
    assert other.count(LOGO) == 1  # another office does not see office A's readings
    assert len(rows(b, "SELECT 1 FROM image_readings")) == 2


def test_re_ingestion_reads_nothing_again(offices):
    a, _ = offices
    ver = ingest(a, R1, ScriptedVision())
    again = ScriptedVision()
    a.readers[a.office_id] = again
    assert pipeline.reindex_version(a.office_id, ver) == "reindexed"
    assert again.calls == []
    read = rows(a, "SELECT status FROM document_blocks WHERE version_id = :v AND kind = 'image'"
                   " AND status IN ('read', 'read_uncertain')", v=ver)
    assert len(read) == 2  # the logo (once for its ten pages) and the table, from the office's readings


def test_a_rate_limited_reading_fails_the_job_and_keeps_the_earlier_reading(offices):
    a, _ = offices
    ver = ingest(a, R1, ScriptedVision())
    reading = ("SELECT block_index, status, text FROM document_blocks WHERE version_id = :v ORDER BY block_index",
               "SELECT chunk_index, text, embedding::text, embedding_model FROM chunks WHERE version_id = :v"
               " ORDER BY chunk_index",
               "SELECT ingestion->>'reading_id' FROM document_versions WHERE id = :v")
    before = [rows(a, sql, v=ver) for sql in reading]
    with tenant_tx(a.system) as conn:  # a reading under a new reader version: nothing cached for it
        conn.execute(text("DELETE FROM image_readings"))
        assert enqueue_reindex(conn, ver, pipeline.PDF_INGESTION_VERSION)
    a.readers[a.office_id] = ScriptedVision(fail={TABLE: "rate_limited"})
    while worker.run_one("test-worker"):
        pass
    job = rows(a, "SELECT status, last_error FROM jobs WHERE payload->>'mode' = 'reindex'")[0]
    assert job.status in ("queued", "failed") and "rate_limited" in job.last_error  # retried later
    after = [rows(a, sql, v=ver) for sql in reading]
    assert after == before  # blocks, chunks with their embeddings, and the reading id: all as they were


def test_invalid_output_twice_leaves_the_region_unread_and_the_job_completes(offices):
    a, _ = offices
    vision = ScriptedVision(fail={TABLE: "invalid"})
    ver = ingest(a, R1, vision)
    assert vision.count(TABLE) == 2
    status = rows(a, "SELECT status, ingestion FROM document_versions WHERE id = :v", v=ver)[0]
    assert status.status in ("ready", "needs_review")
    assert status.ingestion["partial"] is True
    assert status.ingestion["unread"] == [{"media": None, "section": "1. סקר דמי שכירות", "page": 1,
                                           "reason": "הקריאה החזותית נכשלה (invalid)"}]
    # the failure is not cached: the next ingestion tries the model again
    hashes = {r.content_hash for r in rows(a, "SELECT content_hash FROM image_readings")}
    table_hash = rows(a, "SELECT content_hash FROM document_blocks WHERE version_id = :v AND status = 'unread'",
                      v=ver)[0].content_hash
    assert table_hash not in hashes
