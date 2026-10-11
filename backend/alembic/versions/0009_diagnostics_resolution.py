"""How a follow-up was resolved, kept with the message's diagnostics.

``message_diagnostics`` gains ``resolution``: the resolving model's raw parse of a follow-up and the server's
decision on each field it claimed (accepted on the user's words, rejected and why), with the entity lookup's
words and outcome. It tells a model's mis-parse from a correct parse the server rejected. It is diagnostics
like the rest of the row: the message's owner and an office admin read it (the table's existing policies), and
only while every document the row names — now also the documents the lookup found or offered — is visible.
The answer keeps only the validated request.

A downgrade drops the column; the detail is diagnostics only and nothing else reads it.

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE message_diagnostics ADD COLUMN resolution jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE message_diagnostics DROP COLUMN resolution")
