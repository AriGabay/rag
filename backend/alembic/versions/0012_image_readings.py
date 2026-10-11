"""image_readings: an office's readings of picture content, reused across its documents (KTD5).

A picture repeated across reports (a logo, a stamp, a watermark) is read once per office: the reading is keyed by
the content's hash (an image's raw stream bytes, or a rendered vector region), the region reader's version, the
model configuration it was read with (``none`` for OCR only) and the crop scale. Only successful and uncertain
readings are stored, never failures.

The table is content-addressed and has no ``document_id``: it is office-scoped under FORCE RLS on ``app_office()``,
and a restrictive policy limits it to the ``system`` role, so only ingestion (the worker, under the office's system
context) reads or writes it; no user request reaches it.

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
CREATE TABLE image_readings (
  office_id uuid NOT NULL REFERENCES offices(id),
  content_hash text NOT NULL,
  reader_version text NOT NULL,
  model_config text NOT NULL,
  crop_scale text NOT NULL,
  status text NOT NULL CHECK (status IN ('read', 'read_uncertain', 'no_text')),
  reading jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (office_id, content_hash, reader_version, model_config, crop_scale)
)""")
    op.execute("ALTER TABLE image_readings ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE image_readings FORCE ROW LEVEL SECURITY")
    op.execute("CREATE POLICY tenant_isolation ON image_readings USING (office_id = app_office())"
               " WITH CHECK (office_id = app_office())")
    op.execute("CREATE POLICY system_only ON image_readings AS RESTRICTIVE USING (app_role() = 'system')"
               " WITH CHECK (app_role() = 'system')")
    op.execute("GRANT SELECT, INSERT, UPDATE, DELETE ON image_readings TO rag_app")


def downgrade() -> None:
    op.execute("DROP TABLE image_readings")
