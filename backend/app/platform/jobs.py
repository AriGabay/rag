"""PostgreSQL job queue with leases (KTD8).

Several kinds share the queue: ``process`` (ingestion, one per version, and reindexing), ``extract_facts`` (one
attribute from one version, U7), ``extract_measurements`` and ``positions`` (the geometry-only backfill of a version
read before positions existed, KTD3). ``jobs_claim`` serves process jobs first, so no background work starves
ingestion.

A reindex never writes before its gate, so a reindex job that ends without replacing the reading (a regression,
or a transient failure after its bounded attempts) ends as ``kept_previous``, not ``failed`` (KTD9): the version keeps
its current reading and stays available, and its ingestion report records why (``reprocess_kept``). No admin action
is needed; an admin may still re-queue it (``_requeue`` resets ``kept_previous`` jobs too)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import Connection, text

from app.config import get_settings
from app.extraction.default import PDF_MIME
from app.extraction.geometry import POSITIONS_VERSION


def enqueue_processing(conn: Connection, version_id: UUID) -> None:
    """Idempotent: one processing job per version (unique idempotency key)."""
    conn.execute(
        text(
            "INSERT INTO jobs (office_id, version_id, kind, idempotency_key, max_attempts)"
            " VALUES (app_office(), :v, 'process', :k, :m) ON CONFLICT (idempotency_key) DO NOTHING"
        ),
        {"v": version_id, "k": f"process:{version_id}", "m": get_settings().job_max_attempts},
    )


def extract_job_key(version_id: UUID, attribute_id: UUID, extraction_version: str) -> str:
    return f"extract_facts:{version_id}:{attribute_id}:{extraction_version}"


def active_extract_jobs(conn: Connection, attribute_id: UUID, extraction_version: str,
                        version_ids: Sequence[UUID] | None = None) -> dict[UUID, str]:
    """Version -> status of the queued or running extraction jobs of one attribute and extraction version."""
    if version_ids is None:
        where, params = ("payload->>'attribute_id' = :a AND payload->>'extraction_version' = :e",
                         {"a": str(attribute_id), "e": extraction_version})
    else:
        where = "idempotency_key = ANY(:k)"
        params = {"k": [extract_job_key(v, attribute_id, extraction_version) for v in version_ids]}
    return {r.version_id: r.status for r in conn.execute(
        text(f"SELECT version_id, status FROM jobs WHERE kind = 'extract_facts' AND status IN ('queued', 'running')"
             f" AND {where}"), params).all()}


@dataclass
class ExtractEnqueue:
    enqueued: list[UUID] = field(default_factory=list)  # new or reset jobs
    active: list[UUID] = field(default_factory=list)  # already queued or running
    over_cap: list[UUID] = field(default_factory=list)  # no job: the office queue is full


def enqueue_extract_facts(conn: Connection, version_ids: Sequence[UUID], attribute_id: UUID,
                          extraction_version: str, cap: int) -> ExtractEnqueue:
    """Queue one ``extract_facts`` job per version while the office has fewer than ``cap`` queued or running
    extraction jobs. A finished or terminally failed job with the same key is reset to a fresh queued job
    instead of being swallowed by the idempotency key. Serialized per office, so concurrent turns cannot
    overrun the cap."""
    out = ExtractEnqueue()
    if not version_ids:
        return out
    conn.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(app_office()::text, 11))"))
    active = active_extract_jobs(conn, attribute_id, extraction_version, version_ids)
    in_queue = conn.execute(text("SELECT count(*) FROM jobs WHERE kind = 'extract_facts'"
                                 " AND status IN ('queued', 'running')")).scalar_one()
    room = max(0, cap - in_queue)
    payload = json.dumps({"attribute_id": str(attribute_id), "extraction_version": extraction_version})
    for v in version_ids:
        if v in active:
            out.active.append(v)
            continue
        if room <= 0:
            out.over_cap.append(v)
            continue
        row = conn.execute(
            text(
                "INSERT INTO jobs (office_id, version_id, kind, payload, idempotency_key, max_attempts)"
                " VALUES (app_office(), :v, 'extract_facts', CAST(:p AS jsonb), :k, :m)"
                " ON CONFLICT (idempotency_key) DO UPDATE SET status = 'queued', attempts = 0, run_after = now(),"
                " locked_by = NULL, lease_until = NULL, last_error = NULL, payload = EXCLUDED.payload,"
                " max_attempts = EXCLUDED.max_attempts, updated_at = now()"
                " WHERE jobs.status IN ('failed', 'done') RETURNING id"
            ),
            {"v": v, "p": payload, "k": extract_job_key(v, attribute_id, extraction_version),
             "m": get_settings().job_max_attempts},
        ).first()
        if row is None:
            out.active.append(v)
        else:
            out.enqueued.append(v)
            room -= 1
    return out


def renew_lease(conn: Connection, job_id: UUID, worker: str) -> bool:
    return conn.execute(
        text(
            "UPDATE jobs SET lease_until = now() + make_interval(secs => :s), updated_at = now()"
            " WHERE id = :j AND locked_by = :w AND status = 'running' RETURNING id"
        ),
        {"s": get_settings().job_lease_seconds, "j": job_id, "w": worker},
    ).first() is not None


def finish_job(conn: Connection, job_id: UUID) -> None:
    conn.execute(
        text("UPDATE jobs SET status = 'done', lease_until = NULL, last_error = NULL, updated_at = now() WHERE id = :j"),
        {"j": job_id},
    )


ERROR_SHOWN = 500  # characters of a failure kept on the job and on the version


def is_reindex(kind: str, payload: dict | None) -> bool:
    return kind == "process" and (payload or {}).get("mode") == "reindex"


def failure_status(kind: str, payload: dict | None, terminal: bool) -> str:
    """A failed job's next status: queued again while attempts remain; once terminal, ``kept_previous`` for a reindex
    (its current reading was never touched, KTD9) and ``failed`` for everything else."""
    if not terminal:
        return "queued"
    return "kept_previous" if is_reindex(kind, payload) else "failed"


def kept_previous_record(reason: str, attempts: int) -> dict:
    """What a version records when a reprocess kept its reading (``ingestion.reprocess_kept``): why, after how many
    attempts, and when. Shown on the documents screen and in the admin jobs list."""
    return {"reason": reason[:ERROR_SHOWN], "attempts": attempts, "at": datetime.now(UTC).isoformat()}


def record_kept_previous(conn: Connection, version_id: UUID, reason: str, attempts: int) -> None:
    """Record on the version that a reprocess kept its current reading. A later reading that replaces it writes a
    whole new ingestion report, which drops the record."""
    conn.execute(
        text("UPDATE document_versions SET ingestion = COALESCE(ingestion, '{}'::jsonb)"
             " || jsonb_build_object('reprocess_kept', CAST(:k AS jsonb)) WHERE id = :v"),
        {"v": version_id, "k": json.dumps(kept_previous_record(reason, attempts), ensure_ascii=False)},
    )


def fail_job(conn: Connection, job_id: UUID, error: str, permanent: bool, attempts: int, max_attempts: int) -> bool:
    """Record a failure. Returns True when the job is now terminal: ``failed``, or ``kept_previous`` for a reindex,
    whose reason is then recorded on the version too (KTD9)."""
    terminal = permanent or attempts >= max_attempts
    backoff = min(300, 5 * 2 ** max(0, attempts - 1))
    job = conn.execute(text("SELECT kind, payload, version_id FROM jobs WHERE id = :j"), {"j": job_id}).first()
    status = failure_status(job.kind, job.payload, terminal) if job is not None else (
        "failed" if terminal else "queued")
    conn.execute(
        text(
            "UPDATE jobs SET status = :st, last_error = :e, locked_by = NULL, lease_until = NULL,"
            " run_after = now() + make_interval(secs => :b), updated_at = now() WHERE id = :j"
        ),
        {"st": status, "e": error[:ERROR_SHOWN], "b": backoff, "j": job_id},
    )
    if status == "kept_previous" and job.version_id is not None:
        record_kept_previous(conn, job.version_id, error, attempts)
    return terminal


def _requeue(conn: Connection, version_id: UUID, kind: str, key: str, payload: dict) -> bool:
    """Queue a job under ``key``; a finished, failed or ``kept_previous`` job with that key is reset to a fresh queued
    job (an admin's accept of a kept reading re-queues it), a queued or running one is left alone. True when a job
    was queued."""
    return conn.execute(
        text(
            "INSERT INTO jobs (office_id, version_id, kind, payload, idempotency_key, max_attempts)"
            " VALUES (app_office(), :v, :kind, CAST(:p AS jsonb), :k, :m)"
            " ON CONFLICT (idempotency_key) DO UPDATE SET status = 'queued', attempts = 0, run_after = now(),"
            " locked_by = NULL, lease_until = NULL, last_error = NULL, payload = EXCLUDED.payload,"
            " updated_at = now() WHERE jobs.status IN ('failed', 'done', 'kept_previous') RETURNING id"
        ),
        {"v": version_id, "kind": kind, "p": json.dumps(payload), "k": key, "m": get_settings().job_max_attempts},
    ).first() is not None


def enqueue_reindex(conn: Connection, version_id: UUID, ingestion_version: str,
                    accept_regression: bool = False) -> bool:
    """Read a processed version again (blocks, pictures, chunks, embeddings) without touching its records.
    ``accept_regression``: an admin's optional decision to apply a reading an earlier run held back as worse than the
    current one (KTD7, KTD9); the version never waits for it."""
    payload = {"mode": "reindex", "ingestion_version": ingestion_version}
    if accept_regression:
        payload["accept_regression"] = True
    return _requeue(conn, version_id, "process", f"reindex:{version_id}:{ingestion_version}", payload)


def enqueue_measurements(conn: Connection, version_id: UUID, extraction_version: str) -> bool:
    return _requeue(conn, version_id, "extract_measurements", f"measure:{version_id}:{extraction_version}",
                    {"extraction_version": extraction_version})


def positions_job_key(version_id: UUID) -> str:
    return f"positions:{version_id}:{POSITIONS_VERSION}"


def enqueue_positions(conn: Connection, version_id: UUID) -> bool:
    """Queue the geometry-only backfill of one version (KTD3). One job per version and positions scheme: a queued or
    running job is left alone; a finished or failed one is queued again (a file restored after it was missing)."""
    return _requeue(conn, version_id, "positions", positions_job_key(version_id),
                    {"positions_version": POSITIONS_VERSION})


def versions_without_positions(conn: Connection, limit: int | None = None) -> list[UUID]:
    """The office's current PDF versions whose reading predates positions (no ``positions`` marker of the current
    scheme in their ingestion report), oldest first."""
    return list(conn.execute(text(
        "SELECT v.id FROM document_versions v JOIN documents d ON d.id = v.document_id AND d.deleted_at IS NULL"
        " WHERE v.is_current AND v.status IN ('ready', 'needs_review') AND v.mime_type = :pdf"
        " AND (v.ingestion->>'positions') IS DISTINCT FROM :pv ORDER BY v.created_at, v.id LIMIT :n"),
        {"pdf": PDF_MIME, "pv": POSITIONS_VERSION, "n": limit}).scalars())


def enqueue_missing_positions(conn: Connection, limit: int | None = None) -> list[UUID]:
    """Queue the backfill for the office's current PDF versions without positions (lazily: only those, and only when
    asked; it ranks below ingestion in ``jobs_claim``). Returns the versions a job was queued for."""
    return [v for v in versions_without_positions(conn, limit) if enqueue_positions(conn, v)]
