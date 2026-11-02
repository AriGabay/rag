"""document_units: the appraisal units inside a document's current reading (round 9 U7: KTD4, KTD5; R6–R9).

A file may hold several appraisals and appendices; retrieval has to see where each starts and ends and what it is
called, in SQL. A unit is a range of the reading's blocks and pages with a kind (appraisal, appendix, comparables,
front matter, or an undetermined document), a title, document-grounded aliases (normalized, in a GIN-indexed array),
identifying details (addresses, block and parcel), a certainty marker for the boundary that opens it and the
evidence that set it (block indexes and rule names only).

A unit is keyed by the version, the reading it was derived from (``document_versions.ingestion.reading_id``; a
reprocessed version reads again, so its units are replaced in the same transaction) and its number. ``units_version``
and ``derivation_key`` (the derivation's version plus the reading's positions marker) say what derived it: a
positions backfill changes the key, and units of another key are derived again on first use.

Like ``region_readings`` (0014) the table is document-scoped: it carries ``document_id`` under the ``document_access``
policy of 0006, so a unit is visible only through a document the user may see (group permissions included), with
ENABLE + FORCE row security and grants to ``rag_app``. Its write check requires the same visibility.

Downgrade drops the table; units are derived again from the stored reading on first use after an upgrade.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE document_units (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  reading_id text NOT NULL,
  unit_no integer NOT NULL CHECK (unit_no >= 1),
  kind text NOT NULL CHECK (kind IN ('appraisal', 'appendix', 'comparables', 'front_matter', 'document')),
  title text,
  aliases text[] NOT NULL DEFAULT '{}',
  identifiers jsonb NOT NULL DEFAULT '{}'::jsonb,
  page_first integer,
  page_last integer,
  block_first integer NOT NULL,
  block_last integer NOT NULL CHECK (block_last >= block_first),
  certainty text NOT NULL CHECK (certainty IN ('certain', 'probable', 'unsplit')),
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  units_version text NOT NULL,
  derivation_key text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, reading_id, unit_no)
)""")
    op.execute("CREATE INDEX document_units_aliases_idx ON document_units USING gin (aliases)")
    op.execute("CREATE INDEX document_units_office_idx ON document_units (office_id, document_id)")
    op.execute("ALTER TABLE document_units ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_units FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY document_access ON document_units USING (office_id = app_office()"
               " AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))"
               " WITH CHECK (office_id = app_office() AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON document_units TO rag_app")


def downgrade() -> None:
    op.execute("DROP TABLE document_units")
