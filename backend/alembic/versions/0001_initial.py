"""Initial schema: platform, appraisal, answering tables with RLS and lookup functions.

Revision ID: 0001
Revises:
"""

from pathlib import Path

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("0001_initial.sql")

_TABLES = [
    "answer_cache", "answer_sources", "questions", "conversations", "dedup_candidates", "fact_values",
    "occurrences", "transactions", "provider_usage", "audit_events", "jobs", "chunks", "extracted_tables",
    "pages", "document_versions", "documents", "sessions", "user_groups", "document_groups", "users",
    "office_data_versions", "office_settings", "offices",
]
_FUNCTIONS = [
    "bootstrap_office(text, text, text, text)", "docs_hash_lookup(text)", "jobs_claim(text, integer)",
    "auth_resolve_session(text)", "auth_login_lookup(text)", "app_role()", "app_user()", "app_office()",
]


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    for fn in _FUNCTIONS[:5]:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
    for table in _TABLES:
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")
    for fn in _FUNCTIONS[5:]:
        op.execute(f"DROP FUNCTION IF EXISTS {fn}")
