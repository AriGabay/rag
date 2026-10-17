"""Removed-claim detail moves out of the answer into its own table.

An answer stored the text of every claim verification removed, with the reason, and the normal message API
returned it. That detail is for diagnosing the engine, not for the user's normal path. It now lives in
``message_diagnostics``, one row per assistant message:

- the message's owner reads it, and an office admin reads it (``app_role()`` admin) — the ``messages``
  policies stay as they are, so chat messages remain per user;
- the row records the documents the answer drew on, and the endpoint shows it only while all of them are
  visible to the reader.

Existing answers are migrated: their verification rounds and problems are copied here, and removed from the
answer, which keeps only counts. The copy runs as the tables' owner with row security not forced, inside this
migration's transaction, and forcing is restored before it commits.

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE message_diagnostics (
  message_id uuid PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
  office_id uuid NOT NULL REFERENCES offices(id),
  user_id uuid NOT NULL REFERENCES users(id),
  rounds jsonb NOT NULL DEFAULT '[]'::jsonb,
  removed jsonb NOT NULL DEFAULT '[]'::jsonb,
  document_ids text[] NOT NULL DEFAULT '{}',
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX message_diagnostics_office_idx ON message_diagnostics (office_id);
ALTER TABLE message_diagnostics ENABLE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON message_diagnostics
  USING (office_id = app_office()) WITH CHECK (office_id = app_office());
CREATE POLICY owner_or_admin ON message_diagnostics AS RESTRICTIVE
  USING (user_id = app_user() OR app_role() IN ('admin', 'system'))
  WITH CHECK (user_id = app_user() OR app_role() = 'system');
GRANT SELECT, INSERT, UPDATE, DELETE ON message_diagnostics TO rag_app;

ALTER TABLE messages NO FORCE ROW LEVEL SECURITY;
INSERT INTO message_diagnostics (message_id, office_id, user_id, rounds, removed, document_ids)
SELECT m.id, m.office_id, m.user_id,
       COALESCE(m.answer->'verification'->'rounds', '[]'::jsonb),
       COALESCE(m.answer->'verification'->'problems', '[]'::jsonb),
       ARRAY(SELECT DISTINCT x FROM (
         SELECT s->>'document_id' AS x FROM jsonb_array_elements(COALESCE(m.answer->'sources', '[]'::jsonb)) s
         UNION SELECT jsonb_array_elements_text(COALESCE(m.answer->'touched_documents', '[]'::jsonb))
       ) d WHERE x IS NOT NULL)
FROM messages m
WHERE m.role = 'assistant' AND jsonb_typeof(m.answer->'verification') = 'object';
UPDATE messages SET answer = answer
  || jsonb_build_object('verification', (answer->'verification') - 'rounds' - 'problems'
     || jsonb_build_object(
       'removed', (SELECT count(*) FROM jsonb_array_elements(COALESCE(answer->'verification'->'problems', '[]'::jsonb)) p
                   WHERE p->>'severity' = 'error'),
       'partial', (SELECT count(*) FROM jsonb_array_elements(COALESCE(answer->'verification'->'problems', '[]'::jsonb)) p
                   WHERE p->>'severity' = 'partial'),
       'annotated', 0))
WHERE role = 'assistant' AND jsonb_typeof(answer->'verification') = 'object';
ALTER TABLE messages FORCE ROW LEVEL SECURITY;
ALTER TABLE message_diagnostics FORCE ROW LEVEL SECURITY;
""")


def downgrade() -> None:
    # the detail goes back into the answers before the table goes, so a downgrade loses nothing
    op.execute("""
ALTER TABLE messages NO FORCE ROW LEVEL SECURITY;
ALTER TABLE message_diagnostics NO FORCE ROW LEVEL SECURITY;
UPDATE messages m SET answer = m.answer || jsonb_build_object('verification',
  ((m.answer->'verification') - 'removed' - 'partial' - 'annotated' - 'request_mismatch')
  || jsonb_build_object('rounds', d.rounds, 'problems', d.removed))
FROM message_diagnostics d
WHERE d.message_id = m.id AND jsonb_typeof(m.answer->'verification') = 'object';
ALTER TABLE messages FORCE ROW LEVEL SECURITY;
DROP TABLE message_diagnostics;
""")
