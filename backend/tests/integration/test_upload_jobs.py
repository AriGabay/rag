"""Upload, duplicate handling, job queue, worker retries (U4)."""

import threading

import pytest
from sqlalchemy import text

from app import worker
from app.db import TenantContext, tenant_tx
from app.extraction.base import ChunkResult, ExtractionResult, PageResult
from app.platform import pipeline
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.helpers import tiny_pdf, upload

pytestmark = pytest.mark.db


class FakeExtractor:
    calls = 0

    def extract(self, data, mime_type, deadline):
        FakeExtractor.calls += 1
        return ExtractionResult(
            page_count=1,
            pages=[PageResult(1, "שומה סינתטית", "text_layer", 0.99, True)],
            tables=[],
            chunks=[ChunkResult(0, "text", [1], "1. מטרת השומה", "שומה סינתטית לבדיקה")],
        )


def fake_publish(ctx, info):
    with tenant_tx(ctx) as conn:
        conn.execute(
            text("UPDATE document_versions SET status = 'ready', is_current = true, processed_at = now() WHERE id = :v"),
            {"v": info.id},
        )
    return "ready"


@pytest.fixture(autouse=True)
def fake_stages(monkeypatch):
    FakeExtractor.calls = 0
    monkeypatch.setattr("app.extraction.pipeline.get_extractor", lambda: FakeExtractor(), raising=False)
    monkeypatch.setattr(pipeline, "publish_stage", fake_publish)


@pytest.fixture
def office(db):
    a = make_office(db, "משרד א", "admin-a@example.test")
    b = make_office(db, "משרד ב", "admin-b@example.test")
    return a, b


def run_all(n=10):
    while n and worker.run_one("test-worker"):
        n -= 1


def count(ctx, sql):
    with tenant_tx(ctx) as conn:
        return conn.execute(text(sql)).scalar()


def test_batch_upload_creates_versions_and_jobs(client, office):
    a, _ = office
    login(client, "admin-a@example.test")
    r = upload(client, [(f"r{i}.pdf", tiny_pdf(f"r{i}")) for i in range(3)], group_id=a.default_group_id)
    assert [x["status"] for x in r.json()["results"]] == ["accepted"] * 3
    assert count(a.ctx(), "SELECT count(*) FROM jobs WHERE status = 'queued'") == 3
    run_all()
    assert count(a.ctx(), "SELECT count(*) FROM document_versions WHERE status = 'ready'") == 3
    docs = client.get("/api/documents").json()["documents"]
    assert {d["current_version"]["status"] for d in docs} == {"ready"}


def test_duplicate_within_office_is_reported_and_other_office_is_independent(client, office):
    a, b = office
    pdf = tiny_pdf("same")
    login(client, "admin-a@example.test")
    first = upload(client, [("a.pdf", pdf)], group_id=a.default_group_id).json()["results"][0]
    again = upload(client, [("a-copy.pdf", pdf)], group_id=a.default_group_id).json()["results"][0]
    assert again["status"] == "duplicate" and again["version_id"] == first["version_id"]
    assert count(a.ctx(), "SELECT count(*) FROM jobs") == 1
    client.post("/api/auth/logout")
    login(client, "admin-b@example.test")
    other = upload(client, [("a.pdf", pdf)], group_id=b.default_group_id).json()["results"][0]
    assert other["status"] == "accepted" and "reason" not in other


def test_rejections_do_not_block_accepted_files(client, office, monkeypatch):
    a, _ = office
    login(client, "admin-a@example.test")
    monkeypatch.setattr("app.platform.documents.get_settings",
                        lambda: type("S", (), {"max_upload_mb": 0, "max_batch_files": 20,
                                               "max_docx_uncompressed_mb": 1})())
    r = upload(client, [("big.pdf", tiny_pdf("big"))], group_id=a.default_group_id).json()["results"][0]
    assert r["status"] == "rejected" and "גדול" in r["reason"]
    monkeypatch.undo()
    res = upload(client, [("fake.pdf", b"MZ not a pdf"), ("ok.pdf", tiny_pdf("ok")), ("x.txt", b"%PDF-1.4")],
                 group_id=a.default_group_id).json()["results"]
    assert [x["status"] for x in res] == ["rejected", "accepted", "rejected"]
    assert "PDF" in res[0]["reason"]


def test_upload_permissions(client, office):
    a, _ = office
    g1, g2 = make_group(a, "G1"), make_group(a, "G2")
    make_user(a, "reader@example.test", [g1], can_upload=False)
    make_user(a, "uploader@example.test", [g1], can_upload=True)
    login(client, "reader@example.test")
    assert upload(client, [("a.pdf", tiny_pdf("a"))], group_id=g1).status_code == 403
    client.post("/api/auth/logout")
    login(client, "uploader@example.test")
    assert upload(client, [("a.pdf", tiny_pdf("a"))], group_id=g2).status_code == 403
    assert upload(client, [("a.pdf", tiny_pdf("a"))], group_id=g1).json()["results"][0]["status"] == "accepted"


def test_hidden_group_copy_is_cloned_without_reprocessing(client, office):
    a, _ = office
    g1, g2 = make_group(a, "G1"), make_group(a, "G2")
    make_user(a, "u1@example.test", [g1], can_upload=True)
    pdf = tiny_pdf("shared")
    login(client, "admin-a@example.test")
    upload(client, [("s.pdf", pdf)], group_id=g2)
    run_all()
    assert FakeExtractor.calls == 1
    client.post("/api/auth/logout")
    login(client, "u1@example.test")
    res = upload(client, [("s.pdf", pdf)], group_id=g1).json()["results"][0]
    assert res["status"] == "accepted" and "G2" not in str(res)
    run_all()
    assert FakeExtractor.calls == 1  # cloned, not re-extracted
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT count(*) FROM chunks")).scalar() == 2
        assert conn.execute(text("SELECT count(*) FROM document_versions WHERE status = 'ready'")).scalar() == 2


def test_deleted_or_failed_copy_does_not_block_reupload(client, office):
    a, _ = office
    pdf = tiny_pdf("again")
    login(client, "admin-a@example.test")
    first = upload(client, [("x.pdf", pdf)], group_id=a.default_group_id).json()["results"][0]
    run_all()
    assert client.delete(f"/api/documents/{first['document_id']}").json() == {"ok": True}
    second = upload(client, [("x.pdf", pdf)], group_id=a.default_group_id).json()["results"][0]
    assert second["status"] == "accepted" and second["document_id"] != first["document_id"]
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("UPDATE document_versions SET status = 'failed' WHERE id = :v"), {"v": second["version_id"]})
    third = upload(client, [("x.pdf", pdf)], group_id=a.default_group_id).json()["results"][0]
    assert third["status"] == "accepted"


def test_concurrent_workers_never_share_a_job(client, office):
    a, _ = office
    login(client, "admin-a@example.test")
    upload(client, [(f"c{i}.pdf", tiny_pdf(f"c{i}")) for i in range(6)], group_id=a.default_group_id)
    claimed = []
    lock = threading.Lock()

    def grab(name):
        while True:
            job = worker.claim(name)
            if job is None:
                return
            with lock:
                claimed.append(job.job_id)

    threads = [threading.Thread(target=grab, args=(f"w{i}",)) for i in range(3)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(claimed) == 6 and len(set(claimed)) == 6


def test_crash_mid_processing_retries_without_duplicates(client, office, monkeypatch):
    a, _ = office
    login(client, "admin-a@example.test")
    res = upload(client, [("crash.pdf", tiny_pdf("crash"))], group_id=a.default_group_id).json()["results"][0]
    original_embed = pipeline.embed_stage
    state = {"fail": True}

    def flaky_embed(ctx, info, deadline):
        if state["fail"]:
            state["fail"] = False
            raise RuntimeError("worker died")
        return original_embed(ctx, info, deadline)

    monkeypatch.setattr(pipeline, "embed_stage", flaky_embed)
    worker.run_one("w")
    with tenant_tx(a.ctx()) as conn:
        status = conn.execute(text("SELECT status FROM document_versions WHERE id = :v"), {"v": res["version_id"]}).scalar()
        assert status == "pending"  # never shown as ready in between
        conn.execute(text("UPDATE jobs SET run_after = now()"))
    worker.run_one("w")
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT status FROM document_versions")).scalar() == "ready"
        assert conn.execute(text("SELECT count(*) FROM pages")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM chunks")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM chunks WHERE embedding IS NULL")).scalar() == 0


def test_expired_lease_is_reclaimed(client, office):
    a, _ = office
    login(client, "admin-a@example.test")
    upload(client, [("l.pdf", tiny_pdf("lease"))], group_id=a.default_group_id)
    assert worker.claim("dead-worker") is not None
    assert worker.claim("other") is None
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("UPDATE jobs SET lease_until = now() - interval '1 second'"))
    job = worker.claim("other")
    assert job is not None and job.attempts == 2


def test_file_endpoint_is_scoped(client, office):
    a, b = office
    login(client, "admin-a@example.test")
    res = upload(client, [("f.pdf", tiny_pdf("file"))], group_id=a.default_group_id).json()["results"][0]
    url = f"/api/documents/{res['document_id']}/versions/{res['version_id']}/file"
    r = client.get(url)
    assert r.status_code == 200 and r.content.startswith(b"%PDF") and r.headers["content-type"] == "application/pdf"
    client.post("/api/auth/logout")
    login(client, "admin-b@example.test")
    assert client.get(url).status_code == 404
    assert client.get("/api/documents/not-a-uuid/versions/x/file").status_code == 404
    _ = TenantContext  # imported for type reference


def test_status_filter_uses_status_parameter(client, office):
    a, _ = office
    login(client, "admin-a@example.test")
    ok = upload(client, [("ok.pdf", tiny_pdf("ok-f"))], group_id=a.default_group_id).json()["results"][0]
    bad = upload(client, [("bad.pdf", tiny_pdf("bad-f"))], group_id=a.default_group_id).json()["results"][0]
    with tenant_tx(a.ctx()) as conn:
        conn.execute(text("UPDATE document_versions SET status = 'failed' WHERE id = :v"), {"v": bad["version_id"]})
    docs = client.get("/api/documents", params={"status": "failed"}).json()["documents"]
    assert [d["id"] for d in docs] == [bad["document_id"]] and ok["document_id"] not in str(docs)


def test_exhausted_job_with_dead_worker_fails_instead_of_looping(client, office):
    a, _ = office
    login(client, "admin-a@example.test")
    res = upload(client, [("poison.pdf", tiny_pdf("poison"))], group_id=a.default_group_id).json()["results"][0]
    assert worker.claim("w-dies") is not None
    with tenant_tx(a.ctx()) as conn:  # the worker was killed on its last allowed attempt
        conn.execute(text("UPDATE jobs SET attempts = max_attempts, lease_until = now() - interval '1 second'"))
    assert worker.claim("w-next") is None
    with tenant_tx(a.ctx()) as conn:
        assert conn.execute(text("SELECT status FROM jobs")).scalar() == "failed"
        version = conn.execute(text("SELECT status, status_reason FROM document_versions WHERE id = :v"),
                               {"v": res["version_id"]}).one()
    assert version.status == "failed" and "מחדש" in version.status_reason
