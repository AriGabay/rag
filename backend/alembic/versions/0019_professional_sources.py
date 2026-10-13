"""professional sources: ``documents.source_kind``, ``document_sections`` and ``standard_profiles`` (round 10 U2:
KTD8, KTD9, KTD10; R1, R16, R17, R20).

A document is an ``appraisal`` (the default: every existing row reads as one, nothing is backfilled), a ``standard``
(an appraisal standard) or ``professional`` (another professional document). The kind is chosen at upload and changed
by an admin; it is never inferred from a name. Visibility is unchanged: ``documents`` keeps its group-based
``document_access`` policy (0006), so a standard is seen exactly as any other document of its group.

``document_sections`` holds a professional document's structure (chapters, sections, definitions, appendices, with the
condition and exception sentences, internal references and modality counts of each), and ``standard_profiles`` one
metadata profile per reading (number, edition, dates, with the quote each value rests on). Both are derived lazily
from the stored blocks (round 10 U4, U5), keyed by the version and the reading they were derived from; a reprocessed
version reads again, so its rows are replaced. They are document-scoped like ``document_units`` (0018): each carries
``document_id`` under the ``document_access`` policy, with ENABLE + FORCE row security, grants to ``rag_app`` and
cascading deletes with the version.

Downgrade drops the two tables and the column; no document, version or reading is touched, and the derivations are
made again on first use after an upgrade. The kind a document had is not kept below this revision.

Revision ID: 0019
Revises: 0018
"""

from alembic import op

revision = "0019"
down_revision = "0018"
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
    op.execute("ALTER TABLE documents ADD COLUMN source_kind text NOT NULL DEFAULT 'appraisal'"
               " CHECK (source_kind IN ('appraisal', 'standard', 'professional'))")
    op.execute("""
CREATE TABLE document_sections (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  reading_id text NOT NULL,
  sections_version text NOT NULL,
  section_no integer NOT NULL CHECK (section_no >= 1),
  label text,
  title text,
  parent_no integer,
  depth integer NOT NULL DEFAULT 0 CHECK (depth >= 0),
  role text NOT NULL CHECK (role IN ('chapter', 'section', 'definitions', 'definition', 'appendix', 'note',
                                     'preamble')),
  block_first integer NOT NULL,
  block_last integer NOT NULL CHECK (block_last >= block_first),
  page_first integer,
  page_last integer,
  limits jsonb NOT NULL DEFAULT '[]'::jsonb,
  refs jsonb NOT NULL DEFAULT '[]'::jsonb,
  modality jsonb NOT NULL DEFAULT '{}'::jsonb,
  evidence jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, reading_id, section_no)
)""")
    op.execute("CREATE INDEX document_sections_office_idx ON document_sections (office_id, document_id)")
    _secure("document_sections")
    op.execute("""
CREATE TABLE standard_profiles (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  reading_id text NOT NULL,
  profile_version text NOT NULL,
  fields jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, reading_id)
)""")
    op.execute("CREATE INDEX standard_profiles_office_idx ON standard_profiles (office_id, document_id)")
    _secure("standard_profiles")


def downgrade() -> None:
    op.execute("DROP TABLE standard_profiles")
    op.execute("DROP TABLE document_sections")
    op.execute("ALTER TABLE documents DROP COLUMN source_kind")
