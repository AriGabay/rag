"""The turn limits a message's turn reached, kept with its diagnostics.

``message_diagnostics`` gains ``limits_hit``: the limits the turn reached (``tool_budget``, ``step_limit``,
``time_limit``, ``inspect_cap``; KTD12). The answer itself says so in a sentence and is marked partial; this keeps
the names for diagnosis and for the evaluation, with no content. It is diagnostics like the rest of the row: the
message's owner and an office admin read it (the table's existing policies, row security stays forced).

A downgrade drops the column; nothing else reads it.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE message_diagnostics ADD COLUMN limits_hit jsonb NOT NULL DEFAULT '[]'::jsonb")


def downgrade() -> None:
    op.execute("ALTER TABLE message_diagnostics DROP COLUMN limits_hit")
