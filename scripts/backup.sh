#!/usr/bin/env bash
# Backup: PostgreSQL (custom-format dump) + the private file volume, into ./backups/<timestamp>/.
# Logically deleted documents stay in both (with deleted_at set); see README "גיבוי ושחזור".
set -euo pipefail
cd "$(dirname "$0")/.."
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT="backups/${STAMP}"
mkdir -p "$OUT"
docker compose exec -T db pg_dump -U postgres -d rag -Fc > "$OUT/rag.dump"
docker compose run --rm --no-deps -v "$PWD/$OUT:/backup" --entrypoint sh worker \
  -c 'tar -C /data/files -czf /backup/files.tar.gz .'
# What the dump holds, for the restore drill and for checking a reprocess (docs/operations/reprocessing.md): the
# schema revision and, per current document version, its reading id and reader version. Ids only, no content.
docker compose exec -T db psql -U postgres -d rag -At -c "SELECT version_num FROM alembic_version" \
  > "$OUT/alembic_version"
docker compose exec -T db psql -U postgres -d rag -At -F "$(printf '\t')" -c \
  "SELECT id, COALESCE(ingestion->>'reading_id', ''), COALESCE(ingestion->>'ingestion_version', '')
   FROM document_versions WHERE is_current ORDER BY id" > "$OUT/readings.tsv"
( cd "$OUT" && shasum -a 256 rag.dump files.tar.gz alembic_version readings.tsv > SHA256SUMS )
echo "backup written to $OUT"
