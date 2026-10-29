#!/usr/bin/env bash
# Isolated real-model stack (round 25, U6, KTD10): a second, standalone compose project next to the office stack.
# See docs/operations/isolated-real-model-run.md.
#
#   scripts/isolated_stack.sh config-check [env-file]   render the config and fail on any office port or volume
#   scripts/isolated_stack.sh up                        config-check, build through migrate, start, wait for the backend
#   scripts/isolated_stack.sh seed [eval|demo|all]      seed office C (eval) and/or the demo offices A and B
#   scripts/isolated_stack.sh status                    containers of the isolated project only
#   scripts/isolated_stack.sh walkthrough-notes         create the private notes file for the walkthrough
#   scripts/isolated_stack.sh down                      stop and remove the isolated containers; volumes are KEPT
#   scripts/isolated_stack.sh purge-volumes <names...>  remove the named isolated volumes (containers must be down)
#
# The environment comes only from real_documents/tests/round25/ops/isolated.env, which must be gitignored
# (git check-ignore). Nothing here prints a secret, and the rendered compose config is never printed.
# It never touches the office project: every compose call pins `-p appraisal-rag-isolated` and `-f` the isolated file.
set -euo pipefail
cd "$(dirname "$0")/.."

PROJECT="appraisal-rag-isolated"
COMPOSE_FILE_ISO="docker-compose.isolated.yml"
OPS_DIR="real_documents/tests/round25/ops"
ENV_FILE="$OPS_DIR/isolated.env"
ISO_VOLUMES=("${PROJECT}_iso_pgdata" "${PROJECT}_iso_filedata")
BACKEND_PORT=18000
DB_PORT=15433

die() { echo "isolated_stack: $*" >&2; exit 1; }

# Refuse an env file that git does not ignore: it holds the provider key and the database secrets.
require_ignored() {
  local f="$1"
  git check-ignore -q -- "$f" || die "$f is not gitignored (git check-ignore failed); refusing to use it"
}

# Compose pinned to the isolated project. Shell values that would override the env file or the project are removed.
dc() {
  local env_file="$1"; shift
  env -u COMPOSE_PROJECT_NAME -u COMPOSE_FILE -u COMPOSE_PROFILES -u COMPOSE_ENV_FILES \
    -u POSTGRES_SUPERUSER_PASSWORD -u RAG_OWNER_PASSWORD -u RAG_APP_PASSWORD -u SESSION_SECRET \
    -u OPENAI_KEY -u OPENAI_API_KEY -u ANTHROPIC_API_KEY -u LLM_PROVIDER -u OPENAI_MODEL \
    -u LLM_TIMEOUT_AGENT_SECONDS -u CHAT_AGENT_OUTPUT_TOKENS -u DEMO_MODE -u EMBEDDING_PROVIDER \
    docker compose -p "$PROJECT" -f "$COMPOSE_FILE_ISO" --env-file "$env_file" "$@"
}

# Value of NAME in the env file, without sourcing it and without echoing it anywhere but the caller's variable.
env_value() {
  local line
  line="$(grep -E "^$1=" "$ENV_FILE" | tail -n 1 || true)"
  printf '%s' "${line#*=}"
}

use_real_env() {
  [ -f "$ENV_FILE" ] || die "$ENV_FILE does not exist; copy $ENV_FILE.example and fill it in (see the runbook)"
  require_ignored "$ENV_FILE"
  if grep -q 'REPLACE_ME' "$ENV_FILE"; then die "$ENV_FILE still holds REPLACE_ME placeholders"; fi
}

config_check() {
  local env_file="${1:-$ENV_FILE}"
  [ -f "$env_file" ] || die "$env_file does not exist"
  require_ignored "$env_file"
  local rendered
  rendered="$(dc "$env_file" config --format json)" || die "docker compose config failed (secrets are not shown)"
  # The rendered config holds secrets: it goes to the checker on stdin and is never printed.
  printf '%s' "$rendered" | python3 -c '
import json, re, sys
cfg = json.load(sys.stdin)
project = sys.argv[1]
errors = []
if cfg.get("name") != project:
    errors.append("project name is %r" % cfg.get("name"))
office_ports = {"5433", "8000", "3000"}
for svc, spec in cfg.get("services", {}).items():
    for p in spec.get("ports") or []:
        if isinstance(p, dict):
            pub = str(p.get("published", ""))
        else:
            parts = str(p).split(":")
            pub = parts[-2] if len(parts) > 1 else parts[0]
        if pub in office_ports:
            errors.append("service %s publishes office host port %s" % (svc, pub))
    if spec.get("image") == "appraisal-rag-backend":
        errors.append("service %s uses the office image name" % svc)
    for v in spec.get("volumes") or []:
        if v.get("type") == "bind" and not str(v.get("source", "")).endswith("/infra/postgres/init"):
            errors.append("service %s binds %s" % (svc, v.get("source")))
vols = cfg.get("volumes", {})
for key, v in vols.items():
    name = (v or {}).get("name", key)
    if not name.startswith(project + "_") or (v or {}).get("external"):
        errors.append("volume %s is %s" % (key, name))
for svc, spec in cfg.get("services", {}).items():
    for v in spec.get("volumes") or []:
        if v.get("type") == "volume" and v.get("source") not in vols:
            errors.append("service %s mounts undeclared volume %s" % (svc, v.get("source")))
raw = json.dumps(cfg)
if re.search(r"appraisal-rag_(pgdata|filedata)", raw) or re.search(r"\b(pgdata|filedata)\b", raw):
    errors.append("an office volume name (appraisal-rag_pgdata / filedata) appears")
if errors:
    print("config-check FAILED:\n  " + "\n  ".join(errors), file=sys.stderr)
    sys.exit(1)
ports = sorted(str(p.get("published")) for s in cfg["services"].values() for p in (s.get("ports") or []))
print("config-check ok: project %s, host ports %s, volumes %s" % (cfg["name"], ", ".join(ports), ", ".join(v["name"] for v in vols.values())))
' "$PROJECT" || exit 1
}

cmd_up() {
  use_real_env
  config_check "$ENV_FILE"
  # Build through the migrate service (it owns the backend image build), then the frontend; force-recreate everything.
  dc "$ENV_FILE" build migrate frontend
  dc "$ENV_FILE" up -d --force-recreate
  echo "waiting for http://localhost:${BACKEND_PORT}/api/health ..."
  for _ in $(seq 1 90); do
    if curl -fsS -o /dev/null "http://localhost:${BACKEND_PORT}/api/health" 2>/dev/null; then
      echo "isolated backend is up on :${BACKEND_PORT}; frontend on :13000; database on :${DB_PORT}"
      echo "next: check a changed constant inside the container (runbook step), then: $0 seed"
      return 0
    fi
    sleep 2
  done
  die "the isolated backend did not answer on :${BACKEND_PORT} within 180 s (see: docker compose -p $PROJECT logs backend)"
}

cmd_seed() {
  use_real_env
  config_check "$ENV_FILE" >/dev/null
  local what="${1:-all}" owner_pw demo_pw
  case "$what" in eval | demo | all) ;; *) die "seed takes eval, demo or all" ;; esac
  owner_pw="$(env_value RAG_OWNER_PASSWORD)"
  demo_pw="$(env_value DEMO_PASSWORD)"
  [ -n "$owner_pw" ] || die "RAG_OWNER_PASSWORD is empty in $ENV_FILE"
  [ -x backend/.venv/bin/python ] || die "backend/.venv/bin/python is missing (run uv sync in backend/)"
  # The marker and the isolated URLs go only to the child process; the seed guard prints the target without secrets.
  seed_one() {
    ( cd backend && env RAG_STACK=isolated BACKEND_URL="http://localhost:${BACKEND_PORT}" \
        OWNER_DATABASE_URL="postgresql+psycopg://rag_owner:${owner_pw}@localhost:${DB_PORT}/rag" \
        ${demo_pw:+DEMO_PASSWORD="$demo_pw"} .venv/bin/python "scripts/$1" )
  }
  if [ "$what" = eval ] || [ "$what" = all ]; then seed_one seed_eval_office.py; fi
  if [ "$what" = demo ] || [ "$what" = all ]; then seed_one seed_demo.py; fi
}

cmd_status() {
  docker ps -a --filter "label=com.docker.compose.project=$PROJECT" --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
}

cmd_walkthrough_notes() {
  local f="$OPS_DIR/walkthrough-$(date +%Y%m%d-%H%M%S).md"
  require_ignored "$OPS_DIR/x"
  mkdir -p "$OPS_DIR"
  {
    echo "# Isolated walkthrough notes (private, no secrets)"
    echo
    echo "Image/constant check inside the container:"
    for step in "upload and processing progress" "question: fact" "question: table" "question: condition" \
      "open a cited source, highlight on the cited block" "follow-up" "topic switch and return" \
      "refresh mid-answer" "refresh after the answer" "retry" "file a report" "export the report as admin" \
      "office B denied: conversation" "office B denied: message" "office B denied: diagnostics" \
      "office B denied: report" "forced failure: failure text" "retry after restoring the limit"; do
      echo "- [ ] $step - result: ; run id: ; screenshot: "
    done
  } > "$f"
  echo "$f"
}

cmd_down() {
  # Containers and the network only. No -v: the volumes stay (purge-volumes removes them, by name).
  require_ignored "$ENV_FILE"
  [ -f "$ENV_FILE" ] || die "$ENV_FILE does not exist"
  dc "$ENV_FILE" down
}

cmd_purge_volumes() {
  [ "$#" -ge 1 ] || die "purge-volumes needs explicit volume names: ${ISO_VOLUMES[*]}"
  local name ok known
  for name in "$@"; do
    ok=0
    for known in "${ISO_VOLUMES[@]}"; do [ "$name" = "$known" ] && ok=1; done
    [ "$ok" = 1 ] || die "'$name' is not an isolated volume (allowed: ${ISO_VOLUMES[*]})"
    docker volume inspect "$name" >/dev/null 2>&1 || die "volume $name does not exist"
    [ "$(docker volume inspect -f '{{ index .Labels "com.docker.compose.project" }}' "$name")" = "$PROJECT" ] \
      || die "volume $name does not carry the label of project $PROJECT"
  done
  if [ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$PROJECT")" ]; then
    die "isolated containers still exist; run '$0 down' first"
  fi
  for name in "$@"; do
    echo "removing isolated volume $name"
    docker volume rm "$name"
  done
}

sub="${1:-}"
[ "$#" -gt 0 ] && shift
case "$sub" in
  config-check) config_check "$@" ;;
  up) cmd_up ;;
  seed) cmd_seed "$@" ;;
  status) cmd_status ;;
  walkthrough-notes) cmd_walkthrough_notes ;;
  down) cmd_down ;;
  purge-volumes) cmd_purge_volumes "$@" ;;
  *) sed -n 2,13p "$0"; exit 2 ;;
esac
