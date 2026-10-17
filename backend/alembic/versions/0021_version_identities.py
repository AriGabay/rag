"""version identities: ``version_identities``, the lazily derived record of the identification facts an appraisal version
states (round 12 U5: KTD1, KTD15; R5, R21).

One row per fact the version's identification parts state, or a marker row (``kind`` 'marker', ``block_index`` -1) that
says the derivation found none, in the mold of ``version_dates`` (0020): ``kind`` is ``parcel`` (``block``, ``parcel``,
``sub_parcel``), ``address`` (``street`` core, ``number``), ``city`` (``city``; ``labelled`` says whether a label named
it), or ``apartment`` (``apartment``); ``written`` is the words as the document writes them, ``section`` where they were
read (the unit's title, an identification section, a labelled line, a table cell), ``page`` the printed-order page of
the block (NULL for a DOCX). The facts belong to blocks, not to units: the owning unit is found at read time from the
current ``document_units``, so a unit re-derivation needs no rewrite and ``document_units.identifiers`` and
``UNITS_VERSION`` are untouched.

The table is document-scoped like ``document_units`` (0018), the tables of 0019 and ``version_dates``: it carries
``document_id`` under the ``document_access`` policy with ENABLE + FORCE row security, grants to ``rag_app`` and cascading
deletes with the version.

The revision is additive. Downgrade drops the table: no document, version, unit, date or conversation row is touched, and
the identities are derived again on first use after an upgrade.

Revision ID: 0021
Revises: 0020
"""

from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

_POLICY = ("CREATE POLICY document_access ON {t} USING (office_id = app_office()"
           " AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))"
           " WITH CHECK (office_id = app_office() AND EXISTS (SELECT 1 FROM documents d WHERE d.id = document_id))")


def _secure(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(_POLICY.format(t=table))
    op.execute(f"GRANT SELECT, INSERT, UPDATE, DELETE ON {table} TO rag_app")


def upgrade() -> None:
    # a short bound on the lock the DDL waits for: a busy table fails the revision (retry later) instead of queueing
    # every chat request behind it
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("""
CREATE TABLE version_identities (
  office_id uuid NOT NULL REFERENCES offices(id),
  document_id uuid NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
  identity_version text NOT NULL,
  block_index integer NOT NULL,
  page integer,
  kind text NOT NULL CHECK (kind IN ('parcel', 'address', 'city', 'apartment', 'marker')),
  block text,
  parcel text,
  sub_parcel text,
  apartment text,
  street text,
  number text,
  city text,
  labelled boolean NOT NULL DEFAULT false,
  written text,
  section text CHECK (section IN ('title', 'identification', 'label', 'table')),
  created_at timestamptz NOT NULL DEFAULT now()
)""")
    op.execute("CREATE INDEX version_identities_version_idx ON version_identities (version_id, identity_version)")
    op.execute("CREATE INDEX version_identities_office_idx ON version_identities (office_id, document_id)")
    _secure("version_identities")


def downgrade() -> None:
    op.execute("DROP TABLE version_identities")
