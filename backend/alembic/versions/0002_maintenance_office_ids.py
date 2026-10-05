"""Maintenance helper: list office ids (owner only), for jobs such as re-normalizing search text.

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE FUNCTION maintenance_office_ids() RETURNS SETOF uuid"
        " LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS"
        " $$ SELECT id FROM offices ORDER BY created_at $$"
    )
    op.execute("ALTER FUNCTION maintenance_office_ids() OWNER TO rag_lookup")
    op.execute("REVOKE ALL ON FUNCTION maintenance_office_ids() FROM PUBLIC")
    op.execute("GRANT EXECUTE ON FUNCTION maintenance_office_ids() TO rag_owner")


def downgrade() -> None:
    op.execute("DROP FUNCTION IF EXISTS maintenance_office_ids()")
