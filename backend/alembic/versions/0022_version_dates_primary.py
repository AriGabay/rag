"""version dates: ``version_dates.primary_section`` (round 12 U3b: KTD11; R15, R16).

A valuation date read from the report's own valuation-date section (a block that begins with the label, or sits under a
heading that is the label) is *primary*: when a unit has primary dates and they agree, its other dates are references
and the unit's status is ``stated`` (``chat.dates``). The flag is stored with each derived date so the status is settled
at read time from the stored rows, as the unit assignment already is.

The column is added ``NOT NULL DEFAULT false``, so every row that exists now reads as not primary. Those rows belong to
older ``DATES_VERSION`` values, which the code keeps but never reads (the current one is ``dates-v4``); the dates are
derived again on first use, so no stored value is rewritten and none is lost.

That held at the first deploy only. After a downgrade and a re-upgrade the current-version rows still exist, the column
is back with every flag false, and a version with any row of the current ``DATES_VERSION`` is never derived again: the
lost flags would be read as current. So the downgrade *relabels* (round 13 U13, KTD17; R21, R22) the rows written by the
versions that know the column (everything except ``dates-v1`` and ``dates-v2``) to ``<version>-rolled-back``, before the
column is dropped. A re-upgrade then finds no current row, derives the dates again beside the relabelled ones, and
nothing reads those. Nothing is deleted: the relabel is an UPDATE, the row count is unchanged, and a row already
relabelled is not relabelled again. The code moved to ``dates-v5`` for the databases that cycled before this fix (their
dates-v4 rows may carry lost flags; they stay, unread).

The revision is additive. Downgrade relabels derived rows and drops the column; it deletes nothing.

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
    # FORCE RLS binds the owner too, so the forced policy would match no row: lift it for the UPDATE and restore it in
    # the same transaction (the bracket migration 0016 uses). The suffix is appended once: relabelled rows are skipped.
    op.execute("ALTER TABLE version_dates NO FORCE ROW LEVEL SECURITY")
    op.execute("UPDATE version_dates SET dates_version = dates_version || '-rolled-back'"
               " WHERE dates_version NOT IN ('dates-v1', 'dates-v2') AND dates_version NOT LIKE '%-rolled-back'")
    op.execute("ALTER TABLE version_dates FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE version_dates DROP COLUMN primary_section")
