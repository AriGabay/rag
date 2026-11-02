---
title: Under FORCE RLS the trigram search cannot use its GIN index, and the planner multiplies the predicate
date: 2026-10-12
category: database-issues
module: backend search (platform/search.py trigram stage)
problem_type: performance_issue
component: database
symptoms:
  - "One chat search call takes 10-34 s on an office of a few thousand chunks, almost all of it in the trigram stage"
  - "EXPLAIN as the application role shows the trigram predicate evaluated in hundreds of nested-loop iterations"
  - "The GIN trigram index exists but is never chosen for the application role, even with enable_seqscan off"
root_cause: wrong_api
resolution_type: migration
severity: high
tags: [postgres, row-level-security, force-rls, pg-trgm, gin-index, leakproof, security-definer, query-planner, search-latency]
---

# Under FORCE RLS the trigram search cannot use its GIN index, and the planner multiplies the predicate

## Problem

The chat's fused search runs a lexical, a trigram and a semantic stage over `chunks`, which has `FORCE ROW LEVEL SECURITY` (office and group policies). With fewer than 4,300 chunks, one search call took 10-34 s. Turns ran out of time before reading anything, and the turn time looked like "the model is slow" because model latency was only a third of it.

## Root cause (two separate effects)

1. **Plan shape, not the predicate.** `EXPLAIN (ANALYZE, BUFFERS)` as the application role showed a nested loop. The RLS subplans made the planner estimate one row for `documents` and `document_versions`, so it joined `documents` before `chunks` and evaluated `word_similarity(:q, normalized_text)` once per version × document pair: 256 loops at about 42 ms. A bare pass of the same predicate over all chunks cost about 0.68 s. The 10.8 s came from repeating it.
2. **No index path for the application role.** Postgres never uses a non-`LEAKPROOF` operator as an index qual on a table that has security quals. `pg_trgm`'s `<%`, `%>` and `word_similarity() > th` are not leakproof (`pg_proc.proleakproof = false` for `word_similarity_op` and the like). So `chunks_trgm_idx` (GIN, `gin_trgm_ops`) is unusable for `rag_app` however the query is written. With `enable_seqscan` off the planner still picked `chunks_office_idx` and filtered every row. The same query on an RLS-free copy used the GIN index: 166 ms against 549 ms.

## What didn't work

- **Rewriting the predicate as `:q <% normalized_text`.** This fixed the loop count (16 instead of 256, 0.73 s) but still scanned every chunk. That cost grows linearly with the corpus, and long files were about to triple it.
- **Marking the operator `LEAKPROOF`.** It needs a superuser, so it cannot go in an alembic migration run by the owner role. It would also weaken row security for every table and query in the database. Rejected.

## Solution (PR AriGabay/rag#2, round 9)

A `SECURITY DEFINER` candidate lookup owned by `rag_lookup`, the `BYPASSRLS` role that already owns the pre-tenant lookups (see [force-rls-security-definer-lookups-need-bypassrls-owner.md](force-rls-security-definer-lookups-need-bypassrls-owner.md)). It returns ids and similarity only. Content is read only on the join back under RLS.

- Migration `backend/alembic/versions/0017_trigram_candidates.py` creates `trigram_candidates(p_q, p_versions, p_th, p_n)`:
  - It runs as `SECURITY DEFINER`, with `SET search_path = public, pg_temp` and `SET enable_seqscan = off`. The planner's cost estimate for `<%` is wrong: it priced the seq scan below the GIN scan that was three times faster. Turning seq scans off inside the function only is what makes the GIN path the only one.
  - It sets the threshold with `set_config('pg_trgm.word_similarity_threshold', ..., true)`.
  - The `<%` scan runs in its own `MATERIALIZED` CTE. The filters `office_id = app_office() AND version_id = ANY(p_versions)` apply after it, then the function orders by similarity and limits.
  - `rag_lookup` gets column-level `SELECT` on `chunks(id, office_id, version_id, normalized_text)`, never the passage `text`.
  - `EXECUTE` is revoked from `PUBLIC` and granted to `rag_app`.
- `backend/app/platform/search.py` (`_trigram_statement`) passes the version ids it already resolved under RLS. It joins the returned ids back to `chunks`, `document_versions` and `documents` as `rag_app`, so office and group policies still decide what is read.
- Tests:
  - `backend/tests/integration/test_search.py`: candidate parity with the old predicate, identical fused order, the index plan inside the function, ids and similarity as the only columns, another office's version ids returning nothing, and a hidden-group version yielding nothing after the join back.
  - `backend/tests/integration/test_migration_0017.py`: owner, grants and downgrade.

## Prevention

- Before fixing a slow query on an RLS table, run `EXPLAIN (ANALYZE, BUFFERS)` twice: once as the application role inside `tenant_tx`, once as the owner. The difference separates the cost of the predicate from the cost the security quals add, through misestimated joins or a lost index path.
- Any operator a fast path needs on an RLS table must be leakproof (check `pg_proc.proleakproof`), or the lookup must move into a narrow `SECURITY DEFINER` function that returns ids only and enforces the tenant key itself.
- Known cost caveat: the GIN scan inside the function covers every office's chunks before the office filter. Latency scales with the whole database, not the caller's office. This is acceptable for one office; revisit it when offices multiply.
