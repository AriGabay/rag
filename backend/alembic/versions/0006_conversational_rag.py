"""Conversational RAG: document blocks and ingestion report, measurements with their meaning, chat messages
with progress and cancellation, conversation archive and summary.

Revision ID: 0006
Revises: 0005
"""

from pathlib import Path

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

_SQL = Path(__file__).with_name("0006_conversational_rag.sql")


def upgrade() -> None:
    op.execute(_SQL.read_text(encoding="utf-8"))


def downgrade() -> None:
    op.execute("DROP TABLE messages")
    op.execute("ALTER TABLE conversations DROP COLUMN engine, DROP COLUMN summary_message_count,"
               " DROP COLUMN summary, DROP COLUMN archived_at")
    # Jobs of the new kind cannot exist under the old CHECK. FORCE RLS binds the owner too, so lift it for this
    # one statement (same transaction) to reach every office's rows.
    op.execute("ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM jobs WHERE kind = 'extract_measurements'")
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check")
    op.execute("ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN ('process', 'extract_facts'))")
    op.execute("DROP TABLE measurement_runs")
    op.execute("DROP TABLE measurements")
    op.execute("ALTER TABLE document_versions DROP COLUMN ingestion")
    op.execute("ALTER TABLE chunks NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM chunks WHERE kind IN ('table', 'image')")
    op.execute("ALTER TABLE chunks FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE chunks DROP COLUMN block_end, DROP COLUMN block_start")
    op.execute("ALTER TABLE chunks DROP CONSTRAINT chunks_kind_check")
    op.execute("ALTER TABLE chunks ADD CONSTRAINT chunks_kind_check CHECK (kind IN ('text', 'table_row'))")
    op.execute("DROP TABLE document_blocks")
