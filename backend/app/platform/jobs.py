"""PostgreSQL job queue with leases (KTD8).

Two kinds share the queue: ``process`` (ingestion, one per version) and ``extract_facts`` (one attribute
from one version, U7). ``jobs_claim`` serves process jobs first, so extraction never starves ingestion."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy import Connection, text

from app.config import get_settings


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


def fail_job(conn: Connection, job_id: UUID, error: str, permanent: bool, attempts: int, max_attempts: int) -> bool:
    """Record a failure. Returns True when the job is now terminally failed."""
    terminal = permanent or attempts >= max_attempts
    backoff = min(300, 5 * 2 ** max(0, attempts - 1))
    conn.execute(
        text(
            "UPDATE jobs SET status = :st, last_error = :e, locked_by = NULL, lease_until = NULL,"
            " run_after = now() + make_interval(secs => :b), updated_at = now() WHERE id = :j"
        ),
        {"st": "failed" if terminal else "queued", "e": error[:500], "b": backoff, "j": job_id},
    )
    return terminal


def _requeue(conn: Connection, version_id: UUID, kind: str, key: str, payload: dict) -> bool:
    """Queue a job under ``key``; a finished or failed job with that key is reset to a fresh queued job, a queued
    or running one is left alone. True when a job was queued."""
    return conn.execute(
        text(
            "INSERT INTO jobs (office_id, version_id, kind, payload, idempotency_key, max_attempts)"
            " VALUES (app_office(), :v, :kind, CAST(:p AS jsonb), :k, :m)"
            " ON CONFLICT (idempotency_key) DO UPDATE SET status = 'queued', attempts = 0, run_after = now(),"
            " locked_by = NULL, lease_until = NULL, last_error = NULL, payload = EXCLUDED.payload,"
            " updated_at = now() WHERE jobs.status IN ('failed', 'done') RETURNING id"
        ),
        {"v": version_id, "kind": kind, "p": json.dumps(payload), "k": key, "m": get_settings().job_max_attempts},
    ).first() is not None


def enqueue_reindex(conn: Connection, version_id: UUID, ingestion_version: str,
                    accept_regression: bool = False) -> bool:
    """Read a processed version again (blocks, pictures, chunks, embeddings) without touching its records.
    ``accept_regression``: an admin's decision to accept the regression recorded by an earlier run (KTD7)."""
    payload = {"mode": "reindex", "ingestion_version": ingestion_version}
    if accept_regression:
        payload["accept_regression"] = True
    return _requeue(conn, version_id, "process", f"reindex:{version_id}:{ingestion_version}", payload)


def enqueue_measurements(conn: Connection, version_id: UUID, extraction_version: str) -> bool:
    return _requeue(conn, version_id, "extract_measurements", f"measure:{version_id}:{extraction_version}",
                    {"extraction_version": extraction_version})
