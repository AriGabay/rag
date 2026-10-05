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
( cd "$OUT" && shasum -a 256 rag.dump files.tar.gz > SHA256SUMS )
echo "backup written to $OUT"
