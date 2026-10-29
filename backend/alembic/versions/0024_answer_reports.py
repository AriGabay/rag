"""``answer_reports``: a user's report of a problematic answer, kept as a structural snapshot (round 25 U5: R11, KTD9).

Retry deletes the old assistant message and, through ``ON DELETE CASCADE``, its ``message_diagnostics`` row, so the evidence
of a bad answer would be gone the moment the user asked for a new one. A report copies what diagnosis needs *at report
time*: the message, conversation, cited-source and run ids (the build's commit sha, the model ids) and a **structural**
snapshot of the run's diagnostics, built by an allowlist in ``app.chat.reports`` (codes, statuses, timings, locators,
counts - never a draft, an answer, a quote, a label, a cell, or a failure's free text). The reporter's note is the only free
text in the row.

- ``message_id`` and ``conversation_id`` are ``ON DELETE SET NULL``: a retry or a deleted conversation leaves the report
  (and its snapshot) in place with an empty link. The reports hold no answer or document text, so nothing of a deleted
  conversation stays behind.
- One report per (user, message): a second report of the same message by the same user updates the first
  (``answer_reports_user_message_idx``, partial: the rows whose message is gone are not constrained).
- Row security mirrors ``message_diagnostics`` (0008): office isolation plus a RESTRICTIVE ``owner_or_admin`` policy - the
  reporter reads and writes their own rows, an office admin reads the office's rows (and cannot write another user's).
  ``rag_app`` gets no DELETE: a report is never deleted by the application.

Additive. The downgrade drops the table (created here); no other table, row or conversation is touched.

Revision ID: 0024
Revises: 0023
"""

from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
CREATE TABLE answer_reports (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  office_id uuid NOT NULL REFERENCES offices(id),
  user_id uuid NOT NULL REFERENCES users(id),
  conversation_id uuid REFERENCES conversations(id) ON DELETE SET NULL,
  message_id uuid REFERENCES messages(id) ON DELETE SET NULL,
  category text NOT NULL CHECK (category IN ('wrong', 'missing', 'wrong_source', 'wrong_property', 'other')),
  note text NOT NULL DEFAULT '' CHECK (char_length(note) <= 1000),
  message_status text NOT NULL,
  snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
  build_sha text NOT NULL DEFAULT 'unknown',
  model_ids jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX answer_reports_office_created_idx ON answer_reports (office_id, created_at DESC);
CREATE INDEX answer_reports_message_idx ON answer_reports (message_id) WHERE message_id IS NOT NULL;
CREATE INDEX answer_reports_conversation_idx ON answer_reports (conversation_id) WHERE conversation_id IS NOT NULL;
CREATE UNIQUE INDEX answer_reports_user_message_idx ON answer_reports (user_id, message_id) WHERE message_id IS NOT NULL;
ALTER TABLE answer_reports ENABLE ROW LEVEL SECURITY;
ALTER TABLE answer_reports FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON answer_reports
  USING (office_id = app_office()) WITH CHECK (office_id = app_office());
CREATE POLICY owner_or_admin ON answer_reports AS RESTRICTIVE
  USING (user_id = app_user() OR app_role() IN ('admin', 'system'))
  WITH CHECK (user_id = app_user() OR app_role() = 'system');
GRANT SELECT, INSERT, UPDATE ON answer_reports TO rag_app;
""")


def downgrade() -> None:
    op.execute("DROP TABLE answer_reports")
