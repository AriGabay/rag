---
title: Database tests run inside a Compose service container wipe the office's real database
date: 2026-10-08
category: database-issues
module: backend test suite (backend/tests/conftest.py) and the local Docker Compose stack
problem_type: database_issue
component: database
symptoms:
  - "Every table of the local office database is empty after a test run; users cannot log in"
  - "The test run itself passes"
root_cause: config_error
resolution_type: test_fix
severity: critical
tags: [pytest, docker-compose, test-isolation, truncate, data-loss, backup, pg-restore, restore]
---

# Database tests run inside a Compose service container wipe the office's real database

## Problem

The `backend` and `worker` containers of the Compose stack carry `DATABASE_URL` and `OWNER_DATABASE_URL`
pointing at the real `rag` database (`docker-compose.yml`, `x-backend-env`). The database fixtures of the test
suite migrate their target and truncate every table between tests. Running `pytest` inside one of those
containers, which looks like a convenient way to get the image's dependencies, pointed the fixtures at the live
database and emptied it.

## Symptoms

- After a test run inside the worker container, the documents screen was empty and logins failed.
- Nothing in the test output signalled a problem: the suite simply ran against the database it was given.

## What Didn't Work

- **Relying on the test database name being the default.** The tests read the same settings as the app; inside a
  service container those settings are the office's.
- **`scripts/restore.sh` for a database-only incident.** It also deletes the file volume and unpacks the
  backup's files, so any document uploaded after the backup would lose its source file while the restored
  database still points at the old set. The incident only touched the database.

## Solution

Recovery (database only, files untouched):

```bash
# verify the backup, recreate only the database, restore it keeping owners (the RLS lookup functions must stay
# owned by rag_lookup), then migrate and check a login
( cd backups/<timestamp> && shasum -a 256 -c SHA256SUMS )
docker compose stop backend worker
docker compose exec -T db psql -U postgres -d postgres -c "DROP DATABASE rag;" \
  -c "CREATE DATABASE rag OWNER rag_owner ENCODING 'UTF8' LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8' TEMPLATE template0;"
docker compose exec -T db pg_restore -U postgres -d rag --exit-on-error < backups/<timestamp>/rag.dump
docker compose exec -T db psql -U postgres -d rag \
  -c "GRANT CONNECT ON DATABASE rag TO rag_app; GRANT USAGE ON SCHEMA public TO rag_app, rag_lookup;"
docker compose run --rm migrate
docker compose start backend worker
```

Prevention in code: `backend/tests/conftest.py` refuses to run database tests unless both `DATABASE_URL` and
`OWNER_DATABASE_URL` name the `rag_test` database (`TEST_DATABASE`, `_assert_test_database`, called by the
`alembic_config`, `owner_engine` and `db` fixtures); it exits the run with an explanation instead.

## Why This Works

The guard ties destructive fixtures to a database name no service uses, so the same suite is harmless wherever it
is launched: on the host against `rag_test`, or by mistake inside a container whose settings name `rag`. A
database-only restore returns exactly what was lost and leaves the file volume, the other half of the office's
data, as it was.

## Prevention

- Run database tests on the host only (`cd backend && uv run pytest`), never with `docker compose exec` or
  `docker compose run` against a service; tell delegated agents the same in their instructions.
- Take `scripts/backup.sh` before any operation that rewrites data (reprocessing, migrations, restores).
- Match the restore to the damage: a database-only incident gets a database-only restore; `scripts/restore.sh`
  is for replacing the whole office state, database and files together.

## Related Issues

- `docs/solutions/database-issues/force-rls-security-definer-lookups-need-bypassrls-owner.md` — why a restore must
  keep function owners.
- `docs/operations/reprocessing.md` — the backup taken before a reprocess.
- Auto memory: never run DB pytest inside the compose containers (auto memory [claude]).
