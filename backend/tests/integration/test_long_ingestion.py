"""Long PDFs are read in resumable page windows, and failed versions can be retried (U4, KTD2; R1, R2, R3, R9).

The 1,000-page synthetic file (R9d: 100 appraisals of 10 pages, printed labels restarting per appraisal) goes through
the real worker with the hash embedding provider and no vision model. The other scenarios use its first 50 pages in
10-page windows, so a window, a crash or a deadline is cheap to place. Nothing here is customer data."""

from __future__ import annotations

import io
import resource
import sys
import time
from pathlib import Path
from uuid import UUID

import pypdfium2 as pdfium
import pytest
from sqlalchemy import text

from app import worker
from app.config import get_settings
from app.db import tenant_tx
from app.extraction import pdf, pdf_windows
from app.extraction.base import MSG_DEADLINE, MSG_TOO_MANY_PAGES, ExtractionError
from app.platform import pipeline
from app.platform.storage import LocalStorage, staging_prefix
from tests.conftest import login
from tests.factories import make_group, make_office, make_user
from tests.helpers import upload

pytestmark = pytest.mark.db

R9D = Path(__file__).resolve().parents[1] / "fixtures" / "round9" / "R9d_synthetic_long_file_1000_pages.pdf"
WINDOW = 10
PAGES = 50  # five windows


def first_pages(n: int, tag: str = "") -> bytes:
    """The first ``n`` pages of R9d as a file of their own (``tag`` makes the bytes differ)."""
    src = pdfium.PdfDocument(R9D.read_bytes())
    out = pdfium.PdfDocument.new()
    out.import_pages(src, list(range(n)))
    buf = io.BytesIO()
    out.save(buf)
    data = buf.getvalue()
    return data + (f"\n%{tag}\n".encode() if tag else b"")


@pytest.fixture
def office(db, client):
    a = make_office(db, "משרד א", "admin-a@example.test")
    make_user(a, "emp@example.test", [make_group(a, "קבוצה")], can_upload=True)
    login(client, "admin-a@example.test")
    return a


@pytest.fixture(autouse=True)
def small_windows(monkeypatch):
    monkeypatch.setattr(get_settings(), "ingest_window_pages", WINDOW)


def add(client, office, data: bytes, name: str = "long.pdf") -> tuple[str, str]:
    res = upload(client, [(name, data)], group_id=office.default_group_id).json()["results"][0]
    assert res["status"] == "accepted", res
    return res["document_id"], res["version_id"]


def run_all(limit: int = 30) -> int:
    n = 0
    while n < limit and worker.run_one("w"):
        n += 1
    return n


def release_backoff(office) -> None:
    with tenant_tx(office.system) as conn:
        conn.execute(text("UPDATE jobs SET run_after = now()"))


def version(office, version_id) -> object:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT status, status_reason, page_count, ingestion FROM document_versions"
                                 " WHERE id = :v"), {"v": version_id}).one()


def job(office, version_id) -> object:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT status, attempts, last_error FROM jobs WHERE version_id = :v"),
                            {"v": version_id}).one()


def staged(office, version_id) -> list[str]:
    from app.platform.storage import get_storage

    root = get_storage().root / staging_prefix(office.office_id, UUID(str(version_id)))
    return sorted(p.name for p in root.rglob("*") if p.is_file()) if root.exists() else []


def reading(office, version_id) -> dict:
    """The stored reading without ids: what two readings of one file must share."""
    with tenant_tx(office.system) as conn:
        q = lambda sql: conn.execute(text(sql), {"v": version_id}).all()  # noqa: E731
        return {
            "pages": [tuple(r) for r in q("SELECT page_no, text, method, ok, printed_label, display_width FROM pages"
                                          " WHERE version_id = :v ORDER BY page_no")],
            "blocks": [tuple(r) for r in q("SELECT block_index, kind, page, section_path, label, status, text, bbox,"
                                           " spans FROM document_blocks WHERE version_id = :v ORDER BY block_index")],
            "tables": [tuple(r) for r in q("SELECT table_index, page_start, page_end, structure FROM extracted_tables"
                                           " WHERE version_id = :v ORDER BY table_index")],
            "chunks": [tuple(r) for r in q("SELECT chunk_index, kind, page_list, section, text FROM chunks"
                                           " WHERE version_id = :v ORDER BY chunk_index")],
        }


# --- R1: a 1,000-page file ---------------------------------------------------------------------------------------

@pytest.mark.slow
def test_a_1000_page_file_is_ingested_in_50_page_windows(client, office, monkeypatch, capsys):
    monkeypatch.setattr(get_settings(), "ingest_window_pages", 50)
    marks: list[tuple[float, int | None, str]] = []
    record = pipeline.record_progress
    monkeypatch.setattr(pipeline, "record_progress", lambda ctx, v, done, total, stage: (
        marks.append((time.monotonic(), done, stage)), record(ctx, v, done, total, stage))[1])
    publish = {}
    publish_stage = pipeline.publish_stage

    def timed_publish(ctx, info):
        t = time.monotonic()
        try:
            return publish_stage(ctx, info)
        finally:
            publish["seconds"] = time.monotonic() - t

    monkeypatch.setattr(pipeline, "publish_stage", timed_publish)
    data = R9D.read_bytes()
    doc, ver = add(client, office, data)
    t0 = time.monotonic()
    assert run_all() == 1  # one job: every window, the document steps, the embedding and the publish fit one budget
    wall = time.monotonic() - t0

    v = version(office, ver)
    assert v.status in ("ready", "needs_review") and v.page_count == 1000, (v.status, v.status_reason)
    ing = v.ingestion
    assert ing["pages_done"] == ing["pages_total"] == 1000 and "stage" not in ing and len(ing["reading_attempt"]) == 64
    assert staged(office, ver) == []  # published: nothing staged remains
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT count(*) FROM pages WHERE ok")).scalar() == 1000
        assert conn.execute(text("SELECT count(DISTINCT page) FROM document_blocks")).scalar() == 1000
        # 100 appraisals, each opening with chapter 1 on its own first page
        firsts = conn.execute(text("SELECT page FROM document_blocks WHERE kind = 'heading' AND label = '1'"
                                   " ORDER BY block_index")).scalars().all()
        assert firsts == list(range(1, 1001, 10))
        assert conn.execute(text("SELECT count(*) FROM chunks WHERE embedding IS NULL")).scalar() == 0
    # progress went window by window and the documents API returns it while a version is being read
    reading_pages = [d for _, d, stage in marks if stage == pdf_windows.STAGE_READING]
    assert reading_pages == list(range(0, 1001, 50))
    assert [stage for _, _, stage in marks if stage != pdf_windows.STAGE_READING][-3:] == [
        pdf_windows.STAGE_ASSEMBLING, pipeline.STAGE_SAVING, pipeline.STAGE_PUBLISHING]
    # measurements for the report (pytest -s): wall time, per-window time, the document steps, publish, peak memory
    times = [t for t, _, stage in marks if stage == pdf_windows.STAGE_READING]
    per_window = [b - a for a, b in zip(times, times[1:], strict=False)]
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1 if sys.platform == "darwin" else 1024)
    steps: dict[str, float] = {}
    for (a, _, stage), (b, _, _) in zip(marks, marks[1:], strict=False):
        steps[stage] = steps.get(stage, 0.0) + b - a  # the time until the next report belongs to the stage reported
    with capsys.disabled():
        print("\nseconds per stage (to the next report):", {k: round(v, 1) for k, v in steps.items()})
        print(f"\nR9d 1000 pages, 50-page windows: wall {wall:.0f}s, window median {sorted(per_window)[len(per_window)//2]:.1f}s"
              f" max {max(per_window):.1f}s, after the last window {marks[-1][0] - times[-1]:.0f}s,"
              f" publish {publish['seconds']:.1f}s, peak RSS {rss / 1e6:.0f} MB")
    assert publish["seconds"] < get_settings().job_timeout_seconds
    assert rss < 2_000_000_000


# --- R1: a crash resumes at the next window ----------------------------------------------------------------------

def test_a_crash_after_window_three_resumes_at_window_four_and_gives_the_same_reading(client, office, db, monkeypatch):
    other = make_office(db, "משרד ב", "admin-b@example.test")
    data = first_pages(PAGES)
    calls: list[int] = []
    crashed: list[int] = []
    real = pdf.read_window

    def flaky(doc, data_, start, end, settings, deadline, fix=None, only=None):
        if only is None:
            calls.append(start)
            if len(calls) == 4 and not crashed:
                crashed.append(start)
                raise RuntimeError("worker died")
        return real(doc, data_, start, end, settings, deadline, fix, only)

    monkeypatch.setattr(pdf, "read_window", flaky)
    _, ver = add(client, office, data)
    worker.run_one("w")
    v = version(office, ver)
    key = v.ingestion["reading_attempt"]
    assert v.status == "pending" and calls == [0, 10, 20, 30]  # windows 1-3 done, the 4th died
    assert job(office, ver).attempts == 1
    assert {"w000000.json.gz", "w000001.json.gz", "w000002.json.gz", "state.json"} <= set(staged(office, ver))
    assert "w000003.json.gz" not in staged(office, ver)
    release_backoff(office)
    worker.run_one("w")
    assert calls == [0, 10, 20, 30, 30, 40]  # the next attempt begins at window 4, not at page 1
    v = version(office, ver)
    assert v.status in ("ready", "needs_review") and v.ingestion["reading_attempt"] == key
    assert staged(office, ver) == []

    # an uninterrupted reading of the same file, in another office, is the same reading
    monkeypatch.setattr(pdf, "read_window", real)
    client.post("/api/auth/logout")
    login(client, "admin-b@example.test")
    _, clean = add(client, other, data)
    worker.run_one("w")
    assert reading(other, clean) == reading(office, ver)
    assert reading(office, ver)["blocks"] and len(reading(office, ver)["pages"]) == PAGES


# --- R1: the deadline ends a job at a boundary, never as a failure ------------------------------------------------

class Clock:
    """Time as the windows see it: real time plus an offset that grows by ``step`` after every unit of work."""

    def __init__(self, monkeypatch, step: float = 300.0, start: float = 0.0):
        self.offset, self.step = start, step
        self.on = True
        self.real = time.monotonic
        monkeypatch.setattr(pdf_windows, "_now", lambda: self.real() + self.offset)
        end = pdf_windows._Run.end

        def counted(run):
            end(run)
            if self.on:
                self.offset += self.step

        monkeypatch.setattr(pdf_windows._Run, "end", counted)

    def rewind(self) -> None:
        """A new job: its budget starts again."""
        self.offset = 0.0

    def stop(self) -> None:
        self.on, self.offset = False, 0.0


def test_a_deadline_at_a_window_boundary_requeues_without_consuming_an_attempt(client, office, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_timeout_seconds", 900)
    clock = Clock(monkeypatch)  # 300 s per window: the budget is spent after three
    doc, ver = add(client, office, first_pages(PAGES))
    worker.run_one("w")
    v, j = version(office, ver), job(office, ver)
    assert (j.status, j.attempts) == ("queued", 0)  # the claim's attempt is given back
    assert v.status == "processing" and v.status_reason is None
    assert (v.ingestion["pages_done"], v.ingestion["pages_total"], v.ingestion["stage"]) == (
        30, PAGES, pdf_windows.STAGE_READING)
    shown = client.get(f"/api/documents/{doc}").json()["versions"][0]
    assert shown["status"] == "processing" and shown["progress"] == {
        "stage": pdf_windows.STAGE_READING, "pages_done": 30, "pages_total": PAGES}
    clock.stop()
    run_all()
    v, j = version(office, ver), job(office, ver)
    assert v.status in ("ready", "needs_review") and (j.status, j.attempts) == ("done", 1)
    assert staged(office, ver) == []


def test_a_job_that_finishes_no_unit_consumes_an_attempt_and_ends_failed_with_the_deadline_reason(
        client, office, monkeypatch):
    monkeypatch.setattr(get_settings(), "job_timeout_seconds", 900)
    clock = Clock(monkeypatch, start=10_000.0)  # the budget is spent before the first window
    doc, ver = add(client, office, first_pages(PAGES))
    for attempt in (1, 2):
        worker.run_one("w")
        v, j = version(office, ver), job(office, ver)
        assert (j.status, j.attempts) == ("queued", attempt) and v.status == "pending"
        assert staged(office, ver) == ["state.json"]
        release_backoff(office)
    worker.run_one("w")
    v, j = version(office, ver), job(office, ver)
    assert (j.status, j.attempts) == ("failed", 3)
    assert v.status == "failed" and v.status_reason == MSG_DEADLINE
    assert staged(office, ver) == []  # a terminal failure leaves nothing staged
    # the failure is the retriable kind: an admin processes it again, the file is read this time
    shown = client.get(f"/api/documents/{doc}").json()["versions"][0]
    assert shown["status"] == "failed" and shown["retriable"] is True
    clock.stop()
    assert client.post("/api/admin/reprocess", json={"document_id": doc}).json()["retried"] == [ver]
    run_all()
    assert version(office, ver).status in ("ready", "needs_review")


def test_a_deadline_inside_a_window_ends_that_job_and_the_next_one_continues(client, office, monkeypatch):
    calls: list[int] = []
    tripped: list[int] = []
    real = pdf.read_window

    def slow(doc, data_, start, end, settings, deadline, fix=None, only=None):
        if only is None:
            calls.append(start)
            if len(calls) == 3 and not tripped:
                tripped.append(start)
                raise ExtractionError(MSG_DEADLINE, True)  # what ``check_deadline`` raises inside a window
        return real(doc, data_, start, end, settings, deadline, fix, only)

    monkeypatch.setattr(pdf, "read_window", slow)
    _, ver = add(client, office, first_pages(PAGES))
    worker.run_one("w")
    v, j = version(office, ver), job(office, ver)
    assert v.status == "processing" and (j.status, j.attempts) == ("queued", 0)  # windows 1-2 finished: not failed
    assert v.ingestion["pages_done"] == 20
    run_all()
    assert calls == [0, 10, 20, 20, 30, 40]
    assert version(office, ver).status in ("ready", "needs_review")


def test_a_reading_that_cannot_advance_ends_instead_of_looping_on_deadlines(client, office, monkeypatch):
    def always(doc, data_, start, end, settings, deadline, fix=None, only=None):
        raise ExtractionError(MSG_DEADLINE, True)

    monkeypatch.setattr(pdf, "read_window", always)
    _, ver = add(client, office, first_pages(PAGES))
    for _ in range(get_settings().job_max_attempts):
        worker.run_one("w")
        release_backoff(office)
    v = version(office, ver)
    assert job(office, ver).status == "failed" and v.status == "failed" and v.status_reason == MSG_DEADLINE


# --- staged files are purged -------------------------------------------------------------------------------------

def test_a_new_reading_key_deletes_the_staged_files_of_the_old_one(client, office, monkeypatch):
    clock = Clock(monkeypatch, step=900.0)  # one window per job
    _, ver = add(client, office, first_pages(PAGES))
    worker.run_one("w")
    first = version(office, ver).ingestion["reading_attempt"]
    root = LocalStorage(get_settings().file_storage_root).root / str(office.office_id) / "staging" / str(ver)
    assert [p.name for p in root.iterdir()] == [first]
    monkeypatch.setattr(get_settings(), "ingest_window_pages", 20)  # another window size: another reading
    clock.rewind()
    worker.run_one("w")
    second = version(office, ver).ingestion["reading_attempt"]
    assert second != first and [p.name for p in root.iterdir()] == [second]
    assert "w000000.json.gz" in staged(office, ver)
    clock.stop()
    run_all()
    assert staged(office, ver) == [] and version(office, ver).status in ("ready", "needs_review")


def test_deleting_a_document_deletes_what_is_staged_for_it(client, office, monkeypatch):
    Clock(monkeypatch, step=900.0)
    doc, ver = add(client, office, first_pages(PAGES))
    worker.run_one("w")
    assert staged(office, ver)
    assert client.delete(f"/api/documents/{doc}").json() == {"ok": True}
    assert staged(office, ver) == []
    release_backoff(office)
    worker.run_one("w")  # a job for a deleted document does nothing and stages nothing
    assert staged(office, ver) == []


# --- R2: failed versions can be processed again -----------------------------------------------------------------

def test_a_version_failed_by_the_old_page_limit_is_processed_through_admin_into_ready(client, office, db):
    doc, ver = add(client, office, first_pages(12))
    with tenant_tx(office.system) as conn:  # failed as the office's files were on 7 October: 412 pages, limit 300
        conn.execute(text("UPDATE document_versions SET status = 'failed', status_reason = :r WHERE id = :v"),
                     {"r": MSG_TOO_MANY_PAGES.format(n=412, limit=300), "v": ver})
        conn.execute(text("UPDATE jobs SET status = 'failed', attempts = 1"))
    shown = client.get(f"/api/documents/{doc}").json()["versions"][0]
    assert shown["status"] == "failed" and shown["retriable"] is True
    assert not job_is_runnable(office)

    client.post("/api/auth/logout")
    login(client, "emp@example.test")
    assert client.post("/api/admin/reprocess", json={"document_id": doc}).status_code == 403
    client.post("/api/auth/logout")
    login(client, "admin-a@example.test")
    r = client.post("/api/admin/reprocess", json={"document_id": doc}).json()
    assert r["queued"] == 1 and r["retried"] == [ver]
    shown = client.get(f"/api/documents/{doc}").json()["versions"][0]
    assert shown["status"] == "pending" and shown["status_reason"] is None and shown["retriable"] is False
    again = client.post("/api/admin/reprocess", json={"document_id": doc}).json()  # a second click changes nothing
    assert again["queued"] == 0 and again["retried"] == []
    with tenant_tx(office.system) as conn:
        assert conn.execute(text("SELECT count(*) FROM jobs")).scalar() == 1
        retried = conn.execute(text("SELECT count(*) FROM audit_events WHERE action = 'reprocess' AND"
                                    " (details->>'retried')::int = 1")).scalar()
    assert retried == 1

    run_all()
    v = version(office, ver)
    assert v.status in ("ready", "needs_review") and v.ingestion["reading_id"] and v.page_count == 12
    shown = client.get(f"/api/documents/{doc}").json()
    assert shown["id"] == doc and [x["id"] for x in shown["versions"]] == [ver] and shown["versions"][0]["is_current"]


def job_is_runnable(office) -> bool:
    with tenant_tx(office.system) as conn:
        return conn.execute(text("SELECT count(*) FROM jobs WHERE status = 'queued'")).scalar() > 0


def test_a_file_over_the_ceiling_still_fails_with_its_reason_and_is_not_offered_a_retry(client, office, monkeypatch):
    monkeypatch.setattr(get_settings(), "max_pages", 5)
    doc, ver = add(client, office, first_pages(12))
    worker.run_one("w")
    v = version(office, ver)
    assert v.status == "failed" and v.status_reason == MSG_TOO_MANY_PAGES.format(n=12, limit=5)
    assert staged(office, ver) == []
    shown = client.get(f"/api/documents/{doc}").json()["versions"][0]
    assert shown["retriable"] is False
    assert client.post("/api/admin/reprocess", json={"document_id": doc}).json()["queued"] == 0
    assert client.post("/api/admin/reprocess", json={}).json()["retried"] == []
    # once the ceiling is raised the same failure is retriable
    monkeypatch.setattr(get_settings(), "max_pages", 2000)
    assert client.get(f"/api/documents/{doc}").json()["versions"][0]["retriable"] is True
    assert client.post("/api/admin/reprocess", json={}).json()["retried"] == [ver]
    run_all()
    assert version(office, ver).status in ("ready", "needs_review")


def test_a_failure_reading_again_cannot_cure_is_not_retried_and_another_offices_document_is_not_found(
        client, office, db):
    doc, ver = add(client, office, first_pages(3))
    run_all()
    other = make_office(db, "משרד ב", "admin-b@example.test")
    client.post("/api/auth/logout")
    login(client, "admin-b@example.test")
    odoc, over = add(client, other, first_pages(3, "b"))
    with tenant_tx(other.system) as conn:
        conn.execute(text("UPDATE document_versions SET status = 'failed', status_reason = 'הקובץ פגום או שאינו PDF"
                          " תקין' WHERE id = :v"), {"v": over})
    r = client.post("/api/admin/reprocess", json={}).json()
    assert r["retried"] == [] and version(other, over).status == "failed"
    assert client.post("/api/admin/reprocess", json={"document_id": doc}).status_code == 404  # office A's document
    _ = (ver, odoc)
