"""units beside each other by derivation, and ``unit_cards`` (round 15 U3: KTD5, KTD6; R7; KD8).

``document_units`` was keyed ``(version_id, reading_id, unit_no)``: a unit derivation of another key could not live next
to the rows of the current one, so ``units.replace`` and ``units.ensure`` deleted them. Round 15 derives the units again
(boundaries from the subject line) and decides that derived data is added beside the old rows, never removed
(KD8): the key gains ``derivation_key`` (it was already ``NOT NULL`` since 0018, so no row needs a backfill), the readers
filter by the current key, and the rows of the previous derivation stay, read only to resolve a conversation's stored
unit anchors by name or by block range (``entities.live_unit``).

``unit_cards`` holds one identity card per unit, called at ingestion (KTD5): ``(version_id, reading_id, block_first,
block_last, card_version)`` identifies the card, so a re-derivation that keeps a unit's range does not call the model
again. ``state`` is ``pending``, ``done``, ``failed`` or ``empty``; ``fields`` the verified fields (a list of
``{field, quote, block_index}``). The table is document-scoped like ``document_units`` (0018) and ``version_identities``
(0021): it carries ``document_id`` under the ``document_access`` policy with ENABLE + FORCE row security, grants to
``rag_app`` and cascading deletes with the version. ``jobs.kind`` is widened with ``unit_cards`` (the precedent is 0016).

The revision is additive. Downgrade (it deletes no document, version, unit, date, identity or conversation row):

- drops ``unit_cards`` (created here), and removes the queued ``unit_cards`` jobs, which the older ``jobs_kind_check``
  cannot hold (the precedent of 0016; the backfill queues them again);
- restores the old primary key. Several derivations of one unit number cannot share it, so, without deleting anything,
  for each ``(version_id, reading_id, unit_no)`` the newest row (``created_at``, then ``derivation_key``) keeps its
  ``reading_id`` and every other row is *relabelled*: its ``reading_id`` becomes ``<reading_id>#rolled-back#<derivation
  key>``, which no reading has, so no reader sees it (the pattern of 0022). Nothing is lost: a re-upgrade finds those rows
  in place and unread, and the code derives the current ones beside them.

Revision ID: 0023
Revises: 0022
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

_POLICY = ("CREATE POLICY document_access ON {t} USING (office_id = app_office()"
           " AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))"
           " WITH CHECK (office_id = app_office() AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))")

_KINDS_NEW = "('process', 'extract_facts', 'extract_measurements', 'positions', 'unit_cards')"
_KINDS_OLD = "('process', 'extract_facts', 'extract_measurements', 'positions')"


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(_POLICY.format(t=table))
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO rag_app")


def upgrade() -> None:
    # a short bound on the lock the DDL waits for: a busy table fails the revision (retry later) instead of queueing
    # every chat request behind it
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("ALTER TABLE document_units DROP CONSTRAINT document_units_pkey")
    op.execute("ALTER TABLE document_units ADD PRIMARY KEY (version_id, reading_id, derivation_key, unit_no)")
    op.execute("""
CREATE TABLE unit_cards (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  reading_id text NOT NULL,
  block_first integer NOT NULL,
  block_last integer NOT NULL CHECK (block_last >= block_first),
  card_version text NOT NULL,
  state text NOT NULL CHECK (state IN ('pending', 'done', 'failed', 'empty')),
  fields jsonb NOT NULL DEFAULT '[]'::jsonb,
  model text,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (version_id, reading_id, block_first, block_last, card_version)
)""")
    op.execute("CREATE INDEX unit_cards_office_idx ON unit_cards (office_id, document_id)")
    _secure("unit_cards")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check")
    op.execute(f"ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN {_KINDS_NEW})")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    # jobs of the new kind cannot exist under the old check; FORCE RLS binds the owner too (the bracket 0016 uses)
    op.execute("ALTER TABLE jobs NO FORCE ROW LEVEL SECURITY")
    op.execute("DELETE FROM jobs WHERE kind = 'unit_cards'")
    op.execute("ALTER TABLE jobs FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT jobs_kind_check")
    op.execute(f"ALTER TABLE jobs ADD CONSTRAINT jobs_kind_check CHECK (kind IN {_KINDS_OLD})")
    op.execute("DROP TABLE unit_cards")
    # the old key cannot hold two derivations of one unit number: relabel, never delete (see the module docstring)
    op.execute("ALTER TABLE document_units NO FORCE ROW LEVEL SECURITY")
    op.execute("""
UPDATE document_units u SET reading_id = u.reading_id || '#rolled-back#' || u.derivation_key
FROM (SELECT version_id, reading_id, derivation_key, unit_no,
             row_number() OVER (PARTITION BY version_id, reading_id, unit_no
                                ORDER BY created_at DESC, derivation_key DESC) AS rn
      FROM document_units) ranked
WHERE ranked.rn > 1 AND u.version_id = ranked.version_id AND u.reading_id = ranked.reading_id
  AND u.derivation_key = ranked.derivation_key AND u.unit_no = ranked.unit_no""")
    op.execute("ALTER TABLE document_units FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_units DROP CONSTRAINT document_units_pkey")
    op.execute("ALTER TABLE document_units ADD PRIMARY KEY (version_id, reading_id, unit_no)")
