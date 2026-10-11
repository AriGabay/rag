"""document_blocks: where each block came from (KTD3); pages: a page read partly from its text layer and partly
from its pictures (``mixed``).

- ``bbox``: the block's region on its PDF page, ``[x0, top, x1, bottom]`` in points from the top-left corner;
- ``method``: how its text was obtained (``text_layer``, ``ocr``, ``vision``, ``none`` for a picture not read yet);
- ``reader_version``: the reader that produced it;
- ``content_hash``: a picture's hash over its raw image bytes (the key a reading of it is reused by);
- ``original_text``: the text as extracted, when ``text`` holds a corrected form of it.

All nullable: DOCX blocks and every block from before this revision leave them empty. The table's grants and its
tenant RLS policy are table-level and cover the new columns unchanged. Downgrading drops the columns and restores the
old check as NOT VALID, so pages already read as ``mixed`` stay readable (reprocess them under the older reader).

Revision ID: 0011
Revises: 0010
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE document_blocks ADD COLUMN bbox jsonb, ADD COLUMN method text,"
               " ADD COLUMN reader_version text, ADD COLUMN content_hash text, ADD COLUMN original_text text")
    op.execute("ALTER TABLE pages DROP CONSTRAINT pages_method_check")
    op.execute("ALTER TABLE pages ADD CONSTRAINT pages_method_check"
               " CHECK (method IN ('text_layer', 'ocr', 'mixed', 'docx', 'failed'))")


def downgrade() -> None:
    op.execute("ALTER TABLE pages DROP CONSTRAINT pages_method_check")
    op.execute("ALTER TABLE pages ADD CONSTRAINT pages_method_check"
               " CHECK (method IN ('text_layer', 'ocr', 'docx', 'failed')) NOT VALID")
    op.execute("ALTER TABLE document_blocks DROP COLUMN original_text, DROP COLUMN content_hash,"
               " DROP COLUMN reader_version, DROP COLUMN method, DROP COLUMN bbox")
