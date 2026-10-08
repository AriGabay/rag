"""Source positions (KTD2), value stance (KTD8), verified values (KTD12), kept-previous reprocess jobs and the geometry
backfill job (KTD9, KTD3), and OCR languages in reading-cache keys (KTD11). The schema of a round of work, front-loaded
into one revision.

- ``pages``: the page's geometry as the text layer read it (``mediabox`` and ``cropbox``, ``[x0, y0, x1, y1]`` in PDF
  user space; ``rotation`` in degrees), the size of the rendered page (``display_width``, ``display_height``, points),
  the page number printed on it (``printed_label``) and, when its positions cannot be converted into the frame of the
  rendered page, why (``geometry_issue``). All nullable: DOCX pages and pages read before this revision have none.
- ``document_blocks.spans``: a text block's word spans, ``[line, word, start, end, x0, top, x1, bottom]`` each
  (``app.extraction.geometry``). Table cell boxes live inside ``extracted_tables.structure`` (``rows[k].cell_boxes``,
  ``header_boxes``) and need no column.
- ``measurements.stated_by`` and ``measurements.stance``: who stated a value and how (``adopted``, ``claim``,
  ``proposal``, ``estimate``, ``other``, ``unknown``), both optional.
- ``verified_values``: values verified in chat, cached per version, reading and locator. Document-scoped like
  ``region_readings`` (0014): the ``document_access`` policy, ENABLE + FORCE row security, grants to ``rag_app``.
- ``jobs``: the ``kept_previous`` status (a reprocess that kept the current reading) and the ``positions`` kind (the
  geometry-only backfill).
- ``image_readings`` and ``region_readings``: the configuration component of every key (``model_config``) gains the
  OCR languages, ``<model_config>|ocr=heb+eng`` (the only languages ever configured), so the readings stay reusable
  under keys that now name their OCR languages. Both tables force row security, which binds the owner too, so it is
  lifted for the rewrite (same transaction) to reach every office's rows, as 0006 does.

Downgrading drops what this revision added: jobs of the new kind are deleted and ``kept_previous`` jobs become
``failed`` before the old checks return, and the language suffix is removed again from keys that carry exactly
``|ocr=heb+eng`` (a key written later for other languages keeps its suffix; the older code never matches it).

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

STANCES = "('adopted', 'claim', 'proposal', 'estimate', 'other', 'unknown')"
OCR_SUFFIX = "|ocr=heb+eng"


def upgrade() -> None:
    op.execute("ALTER TABLE pages ADD COLUMN mediabox jsonb, ADD COLUMN cropbox jsonb,"
               " ADD COLUMN rotation smallint CONSTRAINT pages_rotation_check CHECK (rotation >= 0 AND rotation < 360),"
               " ADD COLUMN display_width double precision, ADD COLUMN display_height double precision,"
               " ADD COLUMN printed_label text, ADD COLUMN geometry_issue text")
    op.execute("ALTER TABLE document_blocks ADD COLUMN spans jsonb")
    op.execute(f"ALTER TABLE measurements ADD COLUMN stated_by text, ADD COLUMN stance text"
               f" CONSTRAINT measurements_stance_check CHECK (stance IN {STANCES})")

    op.execute("""
CREATE TABLE verified_values (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  reading_id text NOT NULL,
  locator jsonb NOT NULL,
  value jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, reading_id, locator)
)""")
    op.execute("CREATE INDEX verified_values_office_idx ON verified_values (office_id, document_id)")
    op.execute("ALTER TABLE verified_values ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE verified_values FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY document_access ON verified_values USING (office_id = app_office()"
               " AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))"
               " WITH CHECK (office_id = app_office() AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON verified_values TO rag_app")

    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_status_check")
    op.execute("ALTER TABLE jobs ADD CONSTRAINT jobs_status_check"
               " CHECK (status IN ('queued', 'running', 'done', 'failed', 'kept_previous'))")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check")
    op.execute("ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check"
               " CHECK (kind IN ('process', 'extract_facts', 'extract_measurements', 'positions'))")

    for table in ("image_readings", "region_readings"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"UPDATE {table} SET model_config = model_config || '{OCR_SUFFIX}'"
                   f" WHERE model_config NOT LIKE '%|ocr=%'")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    for table in ("image_readings", "region_readings"):
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"UPDATE {table} SET model_config = left(model_config, length(model_config) - {len(OCR_SUFFIX)})"
                   f" WHERE right(model_config, {len(OCR_SUFFIX)}) = '{OCR_SUFFIX}'")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")

    # Jobs of the new kind and status cannot exist under the old checks; FORCE RLS binds the owner too.
    op.execute("ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM jobs WHERE kind = 'positions'")
    op.execute("UPDATE jobs SET status = 'failed' WHERE status = 'kept_previous'")
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check")
    op.execute("ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check"
               " CHECK (kind IN ('process', 'extract_facts', 'extract_measurements'))")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_status_check")
    op.execute("ALTER TABLE jobs ADD CONSTRAINT jobs_status_check CHECK (status IN ('queued', 'running', 'done', 'failed'))")

    op.execute("DROP TABLE verified_values")
    op.execute("ALTER TABLE measurements DROP COLUMN stance, DROP COLUMN stated_by")
    op.execute("ALTER TABLE document_blocks DROP COLUMN spans")
    op.execute("ALTER TABLE pages DROP COLUMN geometry_issue, DROP COLUMN printed_label, DROP COLUMN display_height,"
               " DROP COLUMN display_width, DROP COLUMN rotation, DROP COLUMN cropbox, DROP COLUMN mediabox")
