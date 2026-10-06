"""PostgreSQL job queue with leases (KTD8)."""

from __future__ import annotations

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
