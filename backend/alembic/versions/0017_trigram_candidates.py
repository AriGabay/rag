"""Trigram candidate lookup that can use the GIN index (round 9 U2, R16, KTD3).

``chunks`` forces row security, and Postgres never uses a non-leakproof operator (``<%``, ``%>``) as an index
condition on a table with security quals, so as ``rag_app`` the trigram index ``chunks_trgm_idx`` is never used and
every search reads every chunk (0.14 ms each). Marking the pg_trgm operators ``LEAKPROOF`` would weaken row security
database-wide; this narrow function does the one scan instead.

``trigram_candidates(q, versions, th, n)`` is ``SECURITY DEFINER``, owned by ``rag_lookup`` (``NOLOGIN BYPASSRLS``, as
the other pre-tenant lookups: see ``docs/solutions/database-issues/force-rls-security-definer-lookups-need-bypassrls-
owner.md``) and executable by ``rag_app`` only. It reads chunks of the caller's office (``office_id = app_office()``)
inside the version ids the caller passes — the versions the caller already resolved under row security — and returns
chunk ids with their similarity, never text. The caller joins the ids back to ``chunks``, ``document_versions`` and
``documents`` under row security for scope and ordering, so content is only ever read under row security, and an id
of a version the caller may not see yields no row there. ``rag_lookup`` gets column-level ``SELECT`` on the three
chunk columns the function reads.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

SIGNATURE = "trigram_candidates(text, uuid[], real, integer)"


def upgrade() -> None:
    op.execute("GRANT SELECT (id, office_id, version_id, normalized_text) ON chunks TO rag_lookup")
    # The planner prices ``<%`` as a cheap operator, so with the cheap ``office_id``/``version_id`` conditions beside it
    # it scans by ``chunks_office_idx`` (or sequentially) and filters. The trigram scan is therefore its own
    # materialized step with sequential scans off for the function's duration, which leaves the GIN index as the only
    # way to evaluate ``<%``; the office and version conditions then apply to its candidates.
    op.execute(
        "CREATE FUNCTION trigram_candidates(p_q text, p_versions uuid[], p_th real, p_n integer)"
        " RETURNS TABLE (chunk_id uuid, sim real)"
        " LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp SET enable_seqscan = off AS"
        " $fn$"
        " BEGIN"
        # the threshold ``<%`` compares against, for this transaction (the operator is what the GIN index serves)
        "   PERFORM set_config('pg_trgm.word_similarity_threshold', p_th::text, true);"
        "   RETURN QUERY"
        "   WITH g AS MATERIALIZED ("
        "     SELECT c.id, c.office_id, c.version_id, word_similarity(p_q, c.normalized_text) AS sim"
        "       FROM chunks c WHERE p_q <% c.normalized_text)"
        "   SELECT g.id, g.sim FROM g"
        "    WHERE g.office_id = app_office() AND g.version_id = ANY(p_versions) AND g.sim > p_th"
        "    ORDER BY g.sim DESC, g.id LIMIT p_n;"
        " END"
        " $fn$"
    )
    op.execute(f"ALTER FUNCTION {SIGNATURE} OWNER TO rag_lookup")
    op.execute(f"REVOKE ALL ON FUNCTION {SIGNATURE} FROM PUBLIC")
    op.execute(f"GRANT EXECUTE ON FUNCTION {SIGNATURE} TO rag_app")


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {SIGNATURE}")
    op.execute("REVOKE SELECT (id, office_id, version_id, normalized_text) ON chunks FROM rag_lookup")
