"""General question engine schema: attribute registry, facts, extraction ledger, conversation state,
turn reservation, provider test results, extract_facts jobs, per-user conversations (KTD6).

Revision ID: 0004
Revises: 0003
"""

from pathlib import Path

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("0004_general_engine.sql")

FAILED_REASON = "העיבוד נכשל שוב ושוב (ייתכן שהקובץ גורם לקריסה). ניתן להעלות אותו מחדש"

# jobs_claim exactly as revision 0003 left it.
_JOBS_CLAIM_0003 = f"""
CREATE FUNCTION jobs_claim(p_worker text, p_lease_seconds integer)
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


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP FUNCTION jobs_claim(text, integer)")
    op.execute(_JOBS_CLAIM_0003)
    op.execute("ALTER FUNCTION jobs_claim(text, integer) OWNER TO rag_lookup")
    op.execute("REVOKE ALL ON FUNCTION jobs_claim(text, integer) FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION jobs_claim(text, integer) TO rag_app")

    op.execute("DROP POLICY user_isolation ON questions")
    op.execute("DROP POLICY user_isolation ON conversations")

    # extract_facts jobs cannot exist under the old CHECK. FORCE RLS binds the owner too, so lift it
    # for this one statement (same transaction) to reach every office's rows.
    op.execute("ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM jobs WHERE kind <> 'process'")
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs DROP COLUMN payload")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check")
    op.execute("ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('process'))")

    op.execute("ALTER TABLE chunks DROP COLUMN row_index, DROP COLUMN table_index")
    op.execute(
        "ALTER TABLE office_settings DROP COLUMN provider_tested_at, DROP COLUMN provider_test_status,"
        " DROP COLUMN provider_test_ok, DROP COLUMN provider_test_model, DROP COLUMN provider_test_provider"
    )
    op.execute("DROP INDEX questions_turn_uq")
    op.execute(
        "ALTER TABLE questions DROP COLUMN facts_versions, DROP COLUMN steps, DROP COLUMN plan,"
        " DROP COLUMN status, DROP COLUMN turn_id"
    )
    op.execute("ALTER TABLE conversations DROP COLUMN state_version, DROP COLUMN state")

    op.execute("DROP TABLE fact_extraction_ledger")
    op.execute("DROP TABLE facts")
    op.execute("DROP TABLE attribute_definitions")
