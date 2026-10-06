"""jobs_claim: never re-claim an exhausted job; fail it (and its version) instead.

A worker process that dies mid-job (OOM, SIGKILL) never reaches the Python failure path, so the
expired-lease branch must enforce max_attempts itself, or a poison document loops forever.

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

FAILED_REASON = "העיבוד נכשל שוב ושוב (ייתכן שהקובץ גורם לקריסה). ניתן להעלות אותו מחדש"

_NEW = f"""
CREATE OR REPLACE FUNCTION jobs_claim(p_worker text, p_lease_seconds integer)
RETURNS TABLE (job_id uuid, office_id uuid, version_id uuid, kind text, attempts integer, max_attempts integer)
LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS
$fn$
BEGIN
  -- Exhausted jobs whose worker died: terminal, and so is their version.
  WITH dead AS (
    UPDATE jobs j SET status = 'failed', locked_by = NULL, lease_until = NULL, updated_at = now(),
           last_error = 'lease expired after max attempts'
     WHERE j.status = 'running' AND j.lease_until < now() AND j.attempts >= j.max_attempts
    RETURNING j.version_id)
  UPDATE document_versions v SET status = 'failed', status_reason = '{FAILED_REASON}', processed_at = now()
    FROM dead WHERE v.id = dead.version_id AND v.status IN ('pending', 'processing');

  RETURN QUERY
  UPDATE jobs j
     SET status = 'running', locked_by = p_worker, attempts = j.attempts + 1,
         lease_until = now() + make_interval(secs => p_lease_seconds), updated_at = now()
   WHERE j.id = (
         SELECT c.id FROM jobs c
          WHERE (c.status = 'queued' AND c.run_after <= now())
             OR (c.status = 'running' AND c.lease_until < now() AND c.attempts < c.max_attempts)
          ORDER BY c.created_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1)
  RETURNING j.id, j.office_id, j.version_id, j.kind, j.attempts, j.max_attempts;
END
$fn$;
"""

_OLD = """
CREATE OR REPLACE FUNCTION jobs_claim(p_worker text, p_lease_seconds integer)
RETURNS TABLE (job_id uuid, office_id uuid, version_id uuid, kind text, attempts integer, max_attempts integer)
LANGUAGE sql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp AS
$$
  UPDATE jobs j
     SET status = 'running', locked_by = p_worker, attempts = j.attempts + 1,
         lease_until = now() + make_interval(secs => p_lease_seconds), updated_at = now()
   WHERE j.id = (
         SELECT c.id FROM jobs c
          WHERE (c.status = 'queued' AND c.run_after <= now())
             OR (c.status = 'running' AND c.lease_until < now())
          ORDER BY c.created_at
          FOR UPDATE SKIP LOCKED
          LIMIT 1)
  RETURNING j.id, j.office_id, j.version_id, j.kind, j.attempts, j.max_attempts
$$;
"""


def upgrade() -> None:
    op.execute("GRANT UPDATE (status, status_reason, processed_at) ON document_versions TO rag_lookup")
    op.execute(_NEW)
    op.execute("ALTER FUNCTION jobs_claim(text, integer) OWNER TO rag_lookup")


def downgrade() -> None:
    op.execute(_OLD)
    op.execute("ALTER FUNCTION jobs_claim(text, integer) OWNER TO rag_lookup")
    op.execute("REVOKE UPDATE (status, status_reason, processed_at) ON document_versions FROM rag_lookup")
