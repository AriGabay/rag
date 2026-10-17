"""Conversation summaries record what they were built from: the documents behind the folded answers, the
permission scope and data version at build time, and the last message folded. A summary without this record
(written before this revision) is never shown to the model again; it is rebuilt from what is allowed now.

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE conversations ADD COLUMN summary_meta jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE conversations DROP COLUMN summary_meta")
