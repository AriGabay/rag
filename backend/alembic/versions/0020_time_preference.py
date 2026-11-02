"""time preference: ``conversations.prefer_recent``, ``messages.time_preference`` and ``version_dates`` (round 11 U5:
KTD1, KTD3, KTD15; R2, R14, R15).

``conversations.prefer_recent`` is the conversation's current switch. The column is added ``NOT NULL DEFAULT false`` so
every conversation that exists now reads off (its behavior does not change silently, R2), and the default is then set to
``true`` so a conversation created from now on starts on. ``messages.time_preference`` (jsonb, NULL for every existing
row) holds the state a message was produced with: ``{"version": 1, "prefer_recent", "anchor_date"}``, written on the
user row at send and copied to the assistant row. NULL is a message from before this round and reads as the switch off.

``version_dates`` is the lazily derived record of the valuation dates a version states (KTD3): one row per stated date,
or a marker row that says the derivation found none. It is document-scoped like ``document_units`` (0018) and the
tables of 0019: it carries ``document_id`` under the ``document_access`` policy with ENABLE + FORCE row security,
grants to ``rag_app`` and cascading deletes with the version.

The revision is additive. Downgrade drops what it added: the table, the message column and the conversation column; no
conversation, message or document row is touched, and the dates are derived again on first use after an upgrade.

Revision ID: 0020
Revises: 0019
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

_POLICY = ("CREATE POLICY document_access ON {t} USING (office_id = app_office()"
           " AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))"
           " WITH CHECK (office_id = app_office() AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))")


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(_POLICY.format(t=table))
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO rag_app")


def upgrade() -> None:
    # a short bound on the lock the ALTERs wait for: a busy table fails the revision (retry later) instead of queueing
    # every chat request behind it
    op.execute("SET LOCAL lock_timeout = '5s'")
    # existing rows read false (the column is added with that default), then new rows default to true
    op.execute("ALTER TABLE conversations ADD COLUMN prefer_recent boolean NOT NULL DEFAULT false")
    op.execute("ALTER TABLE conversations ALTER COLUMN prefer_recent SET DEFAULT true")
    op.execute("ALTER TABLE messages ADD COLUMN time_preference jsonb")
    op.execute("""
CREATE TABLE version_dates (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  dates_version text NOT NULL,
  block_index integer NOT NULL,
  iso text,
  precision text CHECK (precision IN ('day', 'month', 'year')),
  written text,
  status text NOT NULL DEFAULT 'stated' CHECK (status IN ('stated', 'conflict')),
  created_at timestamptz NOT NULL DEFAULT now()
)""")
    op.execute("CREATE INDEX version_dates_version_idx ON version_dates (version_id, dates_version)")
    op.execute("CREATE INDEX version_dates_office_idx ON version_dates (office_id, document_id)")
    _secure("version_dates")


def downgrade() -> None:
    op.execute("DROP TABLE version_dates")
    op.execute("ALTER TABLE messages DROP COLUMN time_preference")
    op.execute("ALTER TABLE conversations DROP COLUMN prefer_recent")
