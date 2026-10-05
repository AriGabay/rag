"""Gate 6 (origin §11): re-uploading, a crash in the middle of processing and a retry never create
duplicates or a partial repository shown as complete.

Uses the real ``DefaultExtractor`` on D1 (2 pages, 6 comparables + the report's own value) and
injects one crash at a time into the real pipeline."""

from __future__ import annotations

import time

import pytest
from sqlalchemy import text

from app import worker
from app.appraisal import publish as publish_module
from app.db import tenant_tx
from app.extraction.default import DefaultExtractor
from app.platform import pipeline
from eval.flows import ask_flow
from eval.truth import docs
from tests.acceptance.conftest import FIXTURES, drain, upload_file
from tests.conftest import login
from tests.factories import make_office

pytestmark = pytest.mark.db

D1 = docs()["D1"]
D1_RECORDS = len(D1["records"])  # 7 occurrences, 7 distinct transactions
QUESTION = "מחירי עסקאות למ״ר בחרוזים שנחתמו ב-2024"
ANSWERS = {"area_type": "net", "property_type": "apartment", "vat_basis": "included"}


@pytest.fixture
def office(db, client):
    a = make_office(db, "משרד א (סינתטי)", "admin-a@demo.test")
    login(client, "admin-a@demo.test")
    return a


@pytest.fixture(scope="module")
def d1_chunks() -> int:
    data = (FIXTURES / D1["filename"]).read_bytes()
    return len(DefaultExtractor().extract(data, "application/pdf", time.monotonic() + 300).chunks)


def counts(office, version_id) -> dict:
    with tenant_tx(office.system) as conn:
        q = lambda sql: conn.execute(text(sql), {"v": version_id}).scalar()  # noqa: E731
        return {
            "pages": q("SELECT count(*) FROM pages WHERE version_id = :v"),
            "chunks": q("SELECT count(*) FROM chunks WHERE version_id = :v"),
            "chunks_unembedded": q("SELECT count(*) FROM chunks WHERE version_id = :v AND embedding IS NULL"),
            "tables": q("SELECT count(*) FROM extracted_tables WHERE version_id = :v"),
            "occurrences": q("SELECT count(*) FROM occurrences WHERE version_id = :v"),
            "transactions": q("SELECT count(*) FROM transactions"),
            "fact_dupes": q("SELECT count(*) FROM (SELECT occurrence_id, field FROM fact_values"
                            " GROUP BY 1, 2 HAVING count(*) > 1) x"),
            "status": q("SELECT status FROM document_versions WHERE id = :v"),
        }


def api_status(client, document_id) -> str:
    return client.get(f"/api/documents/{document_id}").json()["current_version"]["status"]


def expect_complete(office, version_id, d1_chunks):
    c = counts(office, version_id)
    assert c["status"] in ("ready", "needs_review")
    assert (c["pages"], c["chunks"], c["chunks_unembedded"], c["tables"]) == (D1["page_count"], d1_chunks, 0, 1)
    assert (c["occurrences"], c["transactions"], c["fact_dupes"]) == (D1_RECORDS, D1_RECORDS, 0)


def approve_all(client):
    for item in client.get("/api/review/queue").json()["items"]:
        if item["kind"] == "record":
            client.post(f"/api/review/records/{item['id']}/approve", json={})


def retry_now(office):
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE jobs SET run_after = now() WHERE status = 'queued'"))


def test_reupload_same_file_is_a_duplicate(office, client, d1_chunks):
    first = upload_file(client, D1["filename"], group_id=str(office.default_group_id))
    drain()
    again = upload_file(client, D1["filename"], group_id=str(office.default_group_id))
    assert again["status"] == "duplicate" and again["version_id"] == first["version_id"]
    as_version = upload_file(client, D1["filename"], document_id=first["document_id"])
    assert as_version["status"] == "duplicate"
    assert drain() == 0
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT count(*) FROM documents")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM document_versions")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar() == 1
    expect_complete(office, first["version_id"], d1_chunks)


@pytest.mark.parametrize("stage", ["extract", "embed", "publish-midway"])
def test_crash_mid_processing_then_retry_leaves_one_complete_set(office, client, monkeypatch, d1_chunks, stage):
    res = upload_file(client, D1["filename"], group_id=str(office.default_group_id))
    state = {"armed": True, "calls": 0}

    def crash_once():
        if state["armed"]:
            state["armed"] = False
            raise RuntimeError("worker died")

    if stage == "extract":  # dies right after stage 1 committed its pages, tables and chunks
        original_stage = pipeline.extract_stage

        def extract_then_die(ctx, info, deadline):
            original_stage(ctx, info, deadline)
            crash_once()
        monkeypatch.setattr(pipeline, "extract_stage", extract_then_die)
    elif stage == "embed":  # dies after the vectors were written, before publishing
        original_embed = pipeline.embed_stage

        def embed_then_die(ctx, info, deadline):
            original_embed(ctx, info, deadline)
            crash_once()
        monkeypatch.setattr(pipeline, "embed_stage", embed_then_die)
    else:  # inside the publish transaction, after some occurrences/transactions were inserted
        original_find = publish_module.find_uncertain

        def find_then_die(conn, txn_id, facts, version_id):
            state["calls"] += 1
            if state["calls"] == 3:
                crash_once()
            return original_find(conn, txn_id, facts, version_id)
        monkeypatch.setattr(publish_module, "find_uncertain", find_then_die)

    assert worker.run_one("w1") is True
    mid = counts(office, res["version_id"])
    assert mid["status"] == "pending"  # back in the queue, never ready in between
    assert api_status(client, res["document_id"]) not in ("ready", "needs_review")
    assert mid["occurrences"] == 0 and mid["transactions"] == 0  # no partial facts published
    login(client, "admin-a@demo.test")
    partial = ask_flow(client, QUESTION, ANSWERS).answer
    assert partial["kind"] != "numeric" and not partial.get("sources")
    hits = client.get("/api/search", params={"q": "מרפסת שמש הפונה מערבה"}).json()["results"]
    assert hits == []  # chunks of an unpublished version are not searchable

    retry_now(office)
    assert drain() == 1
    expect_complete(office, res["version_id"], d1_chunks)
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT attempts, status FROM jobs")).one() == (2, "done")
    approve_all(client)
    final = ask_flow(client, QUESTION, ANSWERS).answer
    assert final["kind"] == "numeric" and final["numeric"]["record_count"] == 2  # D1-T00, D1-T05


def test_expired_lease_of_a_dead_worker_is_resumed_without_duplicates(office, client, d1_chunks):
    res = upload_file(client, D1["filename"], group_id=str(office.default_group_id))
    job = worker.claim("dead-worker")
    assert job is not None
    # the dead worker got as far as stage 1 before disappearing
    info = pipeline.start_processing(pipeline.system_ctx(job.office_id), job.version_id)
    pipeline.extract_stage(pipeline.system_ctx(job.office_id), info, time.monotonic() + 300)
    assert api_status(client, res["document_id"]) == "processing"
    assert worker.run_one("other") is False  # the lease still protects the job
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE jobs SET lease_until = now() - interval '1 second'"))
    assert worker.run_one("other") is True
    expect_complete(office, res["version_id"], d1_chunks)


def test_failed_file_is_never_shown_as_ready(office, client, monkeypatch):
    bad = upload_file(client, "BAD_synthetic_truncated.pdf", group_id=str(office.default_group_id))
    good = upload_file(client, D1["filename"], group_id=str(office.default_group_id))
    seen = set()

    def always_fail(ctx, info):
        raise RuntimeError("publish keeps failing")

    monkeypatch.setattr(pipeline, "publish_stage", always_fail)
    for _ in range(6):
        worker.run_one("w")
        seen.add(api_status(client, good["document_id"]))
        retry_now(office)
    assert "ready" not in seen and "needs_review" not in seen
    assert api_status(client, good["document_id"]) == "failed"
    assert api_status(client, bad["document_id"]) == "failed"
    doc = client.get(f"/api/documents/{good['document_id']}").json()
    assert doc["current_version"]["status_reason"]
    # nothing of the failed version is searchable or counted, and coverage says so
    assert client.get("/api/search", params={"q": "מרפסת שמש הפונה מערבה"}).json()["results"] == []
    a = ask_flow(client, QUESTION, ANSWERS).answer
    assert a["kind"] != "numeric"
    assert a["coverage"]["docs_failed"] == 2
