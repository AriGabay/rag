"""version dates: ``version_dates.primary_section`` (round 12 U3b: KTD11; R15, R16).

A valuation date read from the report's own valuation-date section (a block that begins with the label, or sits under a
heading that is the label) is *primary*: when a unit has primary dates and they agree, its other dates are references
and the unit's status is ``stated`` (``chat.dates``). The flag is stored with each derived date so the status is settled
at read time from the stored rows, as the unit assignment already is.

The column is added ``NOT NULL DEFAULT false``, so every row that exists now reads as not primary. Those rows belong to
older ``DATES_VERSION`` values, which the code keeps but never reads (the current one is ``dates-v4``); the dates are
derived again on first use, so no stored value is rewritten and none is lost.

The revision is additive. Downgrade drops the column and nothing else.

Revision ID: 0022
Revises: 0021
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # a short bound on the lock the ALTER waits for: a busy table fails the revision (retry later) instead of queueing
    # every chat request behind it
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE version_dates ADD COLUMN primary_section boolean NOT NULL DEFAULT false")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE version_dates DROP COLUMN primary_section")
