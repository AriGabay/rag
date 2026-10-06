---
title: FORCE RLS makes owner-owned SECURITY DEFINER lookups return nothing
date: 2026-10-06
category: database-issues
module: backend database (RLS tenant isolation)
problem_type: database_issue
component: database
symptoms:
  - "Login, session resolution, and job claiming return zero rows although the rows exist"
  - "A pre-tenant lookup works only when run as superuser, never through the app"
  - "A restore that does not keep function owners would break every login the same way"
  - "invalid input syntax for type uuid: \"\" from an RLS policy on a reused pooled connection"
root_cause: missing_permission
resolution_type: migration
severity: high
tags: [postgres, row-level-security, force-rls, security-definer, bypassrls, multi-tenant, pg-restore, guc]
---

# FORCE RLS makes owner-owned SECURITY DEFINER lookups return nothing

## Problem

The appraisal engine isolates offices with PostgreSQL row level security and runs the app as a role that cannot bypass it. A few reads must happen before any office is known: login by email, resolving a session token, and claiming the next job across offices. Those were written as `SECURITY DEFINER` functions owned by the migration role, which seemed enough to let them see past RLS. Under `FORCE ROW LEVEL SECURITY` they silently returned nothing.

## Symptoms

- `auth_login_lookup`, `auth_resolve_session` and `jobs_claim` returned zero rows, so nobody could sign in and the worker never claimed a job. No error was raised.
- A restore with `pg_restore --no-owner --role=rag_owner` would break login the same way, because it hands the functions back to the owner role (expected from the semantics above; the restore script avoids it rather than having hit it).
- Once a pooled connection had used `set_config('app.office_id', …, true)` in an earlier transaction, a later transaction without the setting read it back as `''`. A policy that cast it straight to `uuid` then raised `invalid input syntax for type uuid: ""` instead of matching nothing.

## What Didn't Work

- **Owning the functions with the table-owner/migration role (`rag_owner`).** `SECURITY DEFINER` runs as the function owner, but `FORCE ROW LEVEL SECURITY` applies the policies to the table owner too. Only a superuser or a role with `BYPASSRLS` skips forced policies. With no tenant GUC set, the fail-closed policies matched nothing. Two independent plan reviewers flagged this before the code ran (per this session's review), and the first `login_lookup` test would otherwise have failed with no visible cause.
- **Dropping `FORCE`.** Without `FORCE`, the table owner bypasses RLS everywhere, so a bug in any migration-role code path would read across offices. Isolation would depend on never connecting as the owner.
- **`pg_restore --no-owner`.** It is the usual advice for restoring into another environment, and here it reassigns the lookup functions to the restoring role, which reproduces the first failure.

## Solution

1. Create a dedicated role that never logs in and is allowed to bypass RLS, and let the migration role grant ownership to it (`infra/postgres/init/01-roles.sh:10-11`):

   ```sql
   CREATE ROLE rag_lookup NOLOGIN BYPASSRLS;
   GRANT rag_lookup TO rag_owner;
   ```

2. In the migration, give that role only the privileges its functions need: column-level `SELECT` where a lookup reads a few columns, table-level grants only where a function writes (job claiming updates `jobs`, office bootstrap inserts). Hand it ownership of the narrow `SECURITY DEFINER` functions, each with a pinned `search_path`, and grant execute only to the role that calls each one: the runtime role for request-path lookups, the migration role for ops-only functions such as office bootstrap (`backend/alembic/versions/0001_initial.sql:488-489` and the grants around them):

   ```sql
   GRANT SELECT (id, office_id, email, role, password_hash, is_active) ON users TO rag_lookup;
   CREATE FUNCTION auth_login_lookup(p_email text) RETURNS TABLE (...)
     LANGUAGE sql STABLE SECURITY DEFINER SET search_path = public, pg_temp AS $$ ... $$;
   ALTER FUNCTION auth_login_lookup(text) OWNER TO rag_lookup;
   REVOKE ALL ON FUNCTION auth_login_lookup(text) FROM PUBLIC;
   GRANT EXECUTE ON FUNCTION auth_login_lookup(text) TO rag_app;
   ```

   `ALTER FUNCTION … OWNER TO rag_lookup` needs the migration role to be a member of `rag_lookup` and `rag_lookup` to hold `CREATE` on the schema.

3. Read tenant GUCs through a helper that turns the empty string into `NULL`, so a missing context matches nothing instead of erroring (`0001_initial.sql:7-8`):

   ```sql
   CREATE FUNCTION app_office() RETURNS uuid LANGUAGE sql STABLE AS
   $$ SELECT NULLIF(current_setting('app.office_id', true), '')::uuid $$;
   ```

4. Restore as the superuser and keep owners (`scripts/restore.sh:16`, no `--no-owner`), so the lookups stay owned by `rag_lookup`.

## Why This Works

`FORCE ROW LEVEL SECURITY` is what makes the table owner subject to RLS, so the only way through it is the `BYPASSRLS` attribute (or superuser). Putting that attribute on a `NOLOGIN` role that owns only a handful of narrow functions confines the cross-tenant read to exactly those functions. The runtime role `rag_app` stays `NOBYPASSRLS` and owns nothing, and the migration role stays bound by RLS. The attribute is not inherited through role membership, so granting `rag_lookup` to `rag_owner` lets migrations assign ownership without giving `rag_owner` the bypass.

`current_setting(name, true)` returns `NULL` only while the parameter has never been set in the session. After a transaction-local `set_config` ends, the placeholder persists on that connection with an empty value. `NULLIF(..., '')` makes both states mean "no context".

## Prevention

- Test every lookup function with FORCE RLS on and no GUC set, next to a direct select of the same table that must return zero rows (`backend/tests/integration/test_rls.py`, `test_lookup_functions_work_without_context_while_tables_stay_hidden`).
- Assert at test time that the runtime role has `rolbypassrls = false` and owns no tables (`test_runtime_role_cannot_bypass_rls` in the same file).
- Exercise a backup-and-restore round trip that ends with a login, not just a row count. A restore that loses function ownership looks healthy until someone signs in.
- When a new pre-tenant read is needed (as `maintenance_office_ids` in migration 0002 was), give it the same shape: a `SECURITY DEFINER` function owned by `rag_lookup`, minimal columns, a pinned `search_path`, and execute granted only to the role that needs it.

## Related Issues

- Plan: `docs/plans/2026-10-05-2234-feat-appraisal-knowledge-engine-mvp-plan.md` (KTD3, KTD4, KTD6).
- Architecture, isolation model: `docs/architecture.md`.
