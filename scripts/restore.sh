#!/usr/bin/env bash
# Restore a backup made by scripts/backup.sh into the CURRENT stack's database and file volume.
# Destructive for the target: it replaces the 'rag' database and the file volume contents.
# Usage: scripts/restore.sh backups/<timestamp> --yes
set -euo pipefail
cd "$(dirname "$0")/.."
SRC="${1:?usage: scripts/restore.sh backups/<timestamp> --yes}"
[ "${2:-}" = "--yes" ] || { echo "refusing to restore without --yes (this replaces current data)"; exit 1; }
( cd "$SRC" && shasum -a 256 -c SHA256SUMS )
docker compose stop backend worker
docker compose exec -T db psql -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'rag' AND pid <> pg_backend_pid();" \
  -c "DROP DATABASE IF EXISTS rag;" \
  -c "CREATE DATABASE rag OWNER rag_owner ENCODING 'UTF8' LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8' TEMPLATE template0;"
# Restore as superuser keeping owners: the lookup functions must stay owned by rag_lookup (RLS model).
docker compose exec -T db pg_restore -U postgres -d rag --exit-on-error < "$SRC/rag.dump"
docker compose exec -T db psql -U postgres -d rag -v ON_ERROR_STOP=1 \
  -c "GRANT CONNECT ON DATABASE rag TO rag_app; GRANT USAGE ON SCHEMA public TO rag_app, rag_lookup;"
docker compose run --rm --no-deps -v "$PWD/$SRC:/backup:ro" --entrypoint sh worker \
  -c 'find /data/files -mindepth 1 -delete && tar -C /data/files -xzf /backup/files.tar.gz'
docker compose start backend worker
echo "restored from $SRC"
