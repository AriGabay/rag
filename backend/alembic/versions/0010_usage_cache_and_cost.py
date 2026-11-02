"""provider_usage: the prompt-cache token buckets and the estimated cost of every logged model call (KTD2).

``cached_input_tokens`` (input read from the provider's prompt cache) and ``cache_write_tokens`` (input written
to it) are priced apart from ordinary input; ``cost_usd`` is the estimate at log time from the settings' price
table. All nullable: a bucket the provider did not report, a model the table does not price, and every row from
before this revision stay unknown (null), never zero. The table's grants and its tenant RLS policy are
table-level and cover the new columns unchanged.

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE provider_usage ADD COLUMN cached_input_tokens integer,"
               " ADD COLUMN cache_write_tokens integer, ADD COLUMN cost_usd numeric(14, 8)")


def downgrade() -> None:
    op.execute("ALTER TABLE provider_usage DROP COLUMN cost_usd, DROP COLUMN cache_write_tokens,"
               " DROP COLUMN cached_input_tokens")
