"""region_readings: visual readings the answering model asked for (``inspect``, KTD9).

When the model inspects a region or a page that ingestion did not read (or read only uncertainly, without text),
the server renders it, reads it once with the vision model, and keeps the transcription here so a later turn gets
it without another model call. A reading is keyed by the version, the reading it was made from
(``document_versions.ingestion.reading_id``, KTD7: a reprocessed version reads its regions again), the region
(``block:<index>`` or ``page:<number>``), the inspect reader's version and the model configuration. Only successful
and uncertain readings are stored, never failures.

The table is document-scoped: it carries ``document_id`` under the ``document_access`` policy of 0006, so a reading
is visible only through a document the user may see, with ENABLE + FORCE row security and grants to ``rag_app``.
Its write check also requires that visibility, so a reading is stored only for a document the writer may see.
Deleted documents are filtered by the server (``deleted_at``) before any lookup, as for every derived table.

Revision ID: 0014
Revises: 0013
"""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE region_readings (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  reading_id text NOT NULL,
  region text NOT NULL CHECK (region ~ '^(block|page):[0-9]+$'),
  reader_version text NOT NULL,
  model_config text NOT NULL,
  page integer,
  bbox jsonb,
  status text NOT NULL CHECK (status IN ('read', 'read_uncertain', 'no_text')),
  reading jsonb NOT NULL,
  created_by uuid REFERENCES users(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, reading_id, region, reader_version, model_config)
)""")
    op.execute("CREATE INDEX region_readings_office_idx ON region_readings (office_id, document_id)")
    op.execute("ALTER TABLE region_readings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE region_readings FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY document_access ON region_readings USING (office_id = app_office()"
               " AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))"
               " WITH CHECK (office_id = app_office() AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON region_readings TO rag_app")


def downgrade() -> None:
    op.execute("DROP TABLE region_readings")
