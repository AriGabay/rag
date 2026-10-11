#!/usr/bin/env bash
# Checks, from inside the running backend and worker containers, the path to the model provider layer by layer
# (DNS, TCP, TLS, proxy, HTTPS, the app's connection test). Prints statuses only, never the key.
# See docs/operations/local-model-egress.md.
set -euo pipefail
cd "$(dirname "$0")/.."
for service in backend worker; do
  echo "== ${service}"
  container="$(docker compose ps -q "$service" | head -n 1)"
  if [ -z "$container" ]; then echo "  not running"; continue; fi
  docker exec -i "$container" python - < scripts/model_egress_probe.py
done
