"""provider_usage.status: the call status of every logged model call (KTD3, KTD11), e.g. ``timeout`` for a
judge call that did not return. Nullable: rows from before this revision have no status. The table's
grants and its tenant RLS policy are table-level and cover the new column unchanged.

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE provider_usage ADD COLUMN status text")


def downgrade() -> None:
    op.execute("ALTER TABLE provider_usage DROP COLUMN status")
