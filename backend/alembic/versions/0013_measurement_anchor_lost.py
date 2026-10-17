"""measurements.anchor_lost: a measurement whose place in the document was not found again after reprocessing (KTD7).

A reprocessed version gets a new reading, so its blocks, tables and rows may be renumbered. Each measurement is
re-anchored to the new reading by its value and quote (a table value by its table's caption, headers and row
label). A reviewed measurement that cannot be re-anchored keeps the reviewer's decision, but its positions are
cleared and this column records where it was last anchored (``{"reading_id", "block_index", "table_index",
"row_index", "statement_key", "table"}``): the value is no longer offered as one verified against a cell of the
current reading. NULL: anchored in the current reading.

The column is nullable with no default, so rows written before this revision read as anchored. The table's grants
and its tenant RLS policy are table-level and cover the new column unchanged.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE measurements ADD COLUMN anchor_lost jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE measurements DROP COLUMN anchor_lost")
