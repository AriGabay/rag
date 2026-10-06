"""Document processing worker: claims jobs from the Postgres queue and dispatches them by kind.

``process`` jobs run the ingestion pipeline; ``extract_facts`` jobs extract one attribute from one
version (KTD8). Run with ``python -m app.worker``. Several workers may run; ``jobs_claim`` uses
FOR UPDATE SKIP LOCKED so a job is never processed twice concurrently, and an expired lease
lets another worker resume a crashed job."""

from __future__ import annotations

import logging
import os
import signal
import socket
import threading
import time
import traceback
from uuid import UUID

from sqlalchemy import text

from app.answering import facts
from app.answering.attributes import bump_facts_version
from app.config import get_settings
from app.db import TenantContext, anonymous_tx, tenant_tx
from app.extraction.base import ExtractionError
from app.platform import pipeline
from app.platform.jobs import fail_job, finish_job, renew_lease

logger = logging.getLogger("app.worker")

FAILED_REASON = "העיבוד נכשל ({attempts} ניסיונות). ניתן להעלות את הקובץ מחדש"


class _LeaseKeeper(threading.Thread):
    def __init__(self, office_id: UUID, job_id: UUID, worker_id: str):
        super().__init__(daemon=True)
        self.office_id, self.job_id, self.worker_id = office_id, job_id, worker_id
        self.stop_event = threading.Event()

    def run(self) -> None:
        interval = max(5, get_settings().job_lease_seconds // 3)
        while not self.stop_event.wait(interval):
            try:
                with tenant_tx(pipeline.system_ctx(self.office_id)) as conn:
                    renew_lease(conn, self.job_id, self.worker_id)
            except Exception:  # noqa: BLE001
                logger.warning("lease renewal failed for job %s", self.job_id)


def claim(worker_id: str):
    with anonymous_tx() as conn:
        return conn.execute(
            text("SELECT * FROM jobs_claim(:w, :s)"), {"w": worker_id, "s": get_settings().job_lease_seconds}
        ).first()


def handle_extract_facts(ctx: TenantContext, job, worker_id: str) -> None:
    """One attribute from one version (KTD8), under the office context. A skip (cloud off, missing key,
    version deleted or superseded) makes no provider call and fails the job terminally; a provider error
    writes ledger state ``failed`` and calls only ``fail_job``. A failed extraction job never changes
    ``document_versions.status``. A job that wrote facts bumps that attribute's ``facts_version``."""
    payload = job.payload or {}
    keeper = _LeaseKeeper(job.office_id, job.job_id, worker_id)
    keeper.start()
    try:
        try:
            result = facts.run_extraction_job(ctx, job.version_id, payload)
        finally:
            keeper.stop_event.set()
    except Exception as exc:  # noqa: BLE001
        logger.error("extract job %s failed: %s", job.job_id, type(exc).__name__)
        logger.debug("%s", traceback.format_exc())
        facts.mark_job_failed(ctx, job.version_id, payload, type(exc).__name__)
        with tenant_tx(ctx) as conn:
            fail_job(conn, job.job_id, type(exc).__name__, False, job.attempts, job.max_attempts)
        return
    with tenant_tx(ctx) as conn:
        if result.outcome == "done":
            finish_job(conn, job.job_id)
            if result.wrote and result.attribute_id is not None:
                bump_facts_version(conn, result.attribute_id)
        else:
            fail_job(conn, job.job_id, result.reason or result.outcome, result.permanent, job.attempts,
                     job.max_attempts)
    logger.info("extract job %s: %s (%s)", job.job_id, result.outcome, result.reason)


def run_one(worker_id: str) -> bool:
    """Claim and run one job. Returns False when the queue was empty."""
    job = claim(worker_id)
    if job is None:
        return False
    ctx = pipeline.system_ctx(job.office_id)
    if job.kind == "extract_facts":
        handle_extract_facts(ctx, job, worker_id)
        return True
    keeper = _LeaseKeeper(job.office_id, job.job_id, worker_id)
    keeper.start()
    try:
        try:
            pipeline.process_version(job.office_id, job.version_id)
        finally:
            keeper.stop_event.set()  # always stop renewing before recording the outcome
    except ExtractionError as exc:
        with tenant_tx(ctx) as conn:
            terminal = fail_job(conn, job.job_id, exc.reason, exc.permanent, job.attempts, job.max_attempts)
        if terminal:
            pipeline.mark_failed(ctx, job.version_id, exc.reason)
        else:
            pipeline.mark_retry(ctx, job.version_id)
        logger.info("job %s extraction error (permanent=%s)", job.job_id, exc.permanent)
    except Exception as exc:  # noqa: BLE001
        logger.error("job %s failed: %s", job.job_id, type(exc).__name__)
        logger.debug("%s", traceback.format_exc())
        with tenant_tx(ctx) as conn:
            terminal = fail_job(conn, job.job_id, type(exc).__name__, False, job.attempts, job.max_attempts)
        if terminal:
            pipeline.mark_failed(ctx, job.version_id, FAILED_REASON.format(attempts=job.attempts))
        else:
            pipeline.mark_retry(ctx, job.version_id)
    else:
        with tenant_tx(ctx) as conn:
            finish_job(conn, job.job_id)
    return True


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    if get_settings().embedding_provider == "local":
        from app.providers.embeddings import get_embedding_provider

        get_embedding_provider().warmup()
    logger.info("worker %s started", worker_id)
    while not stopping.is_set():
        try:
            if not run_one(worker_id):
                stopping.wait(1.0)
        except Exception:  # noqa: BLE001
            logger.exception("worker loop error")
            time.sleep(2)


if __name__ == "__main__":
    main()
