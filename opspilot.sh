#!/usr/bin/env bash
# OpsPilot master script: run the whole Docker stack from the repo root.
#
#   ./opspilot.sh up            build + start everything, wait until healthy, print URLs
#   ./opspilot.sh down          stop and remove containers (data volume is kept)
#   ./opspilot.sh restart [svc] restart everything, or one service
#   ./opspilot.sh rebuild <svc> rebuild and recreate one service
#   ./opspilot.sh status        container state + health
#   ./opspilot.sh logs [svc]    follow logs (all services, or one)
#   ./opspilot.sh infra         start infrastructure only (postgres, redis, rabbitmq, toxiproxy, jaeger)
#   ./opspilot.sh seed          seed demo users (identity-service)
#   ./opspilot.sh shell <svc>   open a shell in a running service container
#   ./opspilot.sh clean         down + delete the Postgres volume (asks first; destroys all data)
#
# Ollama is not in compose: it must run on the host (http://localhost:11434).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

COMPOSE=(docker compose -f deploy/docker-compose.yml --env-file .env)
INFRA=(postgres redis rabbitmq toxiproxy jaeger)
OLLAMA_URL="${OLLAMA_URL:-http://localhost:11434}"
WAIT_TIMEOUT="${WAIT_TIMEOUT:-600}"

info() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m  %s\n' "$*" >&2; }
die()  { printf '\033[1;31mxx\033[0m  %s\n' "$*" >&2; exit 1; }

preflight() {
  command -v docker >/dev/null || die "docker not found on PATH"
  docker info >/dev/null 2>&1 || die "Docker daemon is not running (start Docker Desktop)"
  [[ -f .env ]] || die ".env missing: cp .env.example .env and fill in JWT_SECRET / FERNET_KEY"
}

check_ollama() {
  if curl -fsS --max-time 3 "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
    local models
    models="$(curl -fsS --max-time 3 "$OLLAMA_URL/api/tags")"
    for m in deepseek-r1:8b nomic-embed-text; do
      grep -q "\"$m" <<<"$models" || warn "Ollama model '$m' not pulled (ollama pull $m)"
    done
  else
    warn "Ollama not reachable at $OLLAMA_URL: triage will park runs as waiting_llm until it is up"
  fi
}

print_urls() {
  cat <<EOF

  OpsPilot is up
  ─────────────────────────────────────────────
  App (SPA)          http://localhost:8080
  API docs           http://localhost:8000/api/docs
  RabbitMQ           http://localhost:15672
  Jaeger             http://localhost:16686
  Dev mailbox        IMAP localhost:3143 · SMTP localhost:3025
  ─────────────────────────────────────────────
  Logs: ./opspilot.sh logs [service]   Stop: ./opspilot.sh down

EOF
}

need_service() { [[ -n "${1:-}" ]] || die "usage: $0 $2 <service>  (services: $("${COMPOSE[@]}" config --services | tr '\n' ' '))"; }

cmd="${1:-help}"
shift || true

case "$cmd" in
  up|start)
    preflight
    check_ollama
    info "Building and starting all services (waiting up to ${WAIT_TIMEOUT}s for health checks)…"
    if "${COMPOSE[@]}" up -d --build --remove-orphans --wait --wait-timeout "$WAIT_TIMEOUT"; then
      "${COMPOSE[@]}" ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
      print_urls
    else
      warn "Some services did not become healthy:"
      "${COMPOSE[@]}" ps -a --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
      warn "'Name or service not known' in the logs usually means stale networks after a Docker restart: run $0 restart"
      die "inspect with: $0 logs <service>"
    fi
    ;;
  down|stop)
    preflight
    info "Stopping OpsPilot…"
    "${COMPOSE[@]}" down
    ;;
  restart)
    preflight
    if [[ -n "${1:-}" ]]; then
      "${COMPOSE[@]}" restart "$@"
    else
      "${COMPOSE[@]}" down
      exec "$0" up
    fi
    ;;
  rebuild)
    preflight
    need_service "${1:-}" rebuild
    "${COMPOSE[@]}" up -d --build --no-deps --wait --wait-timeout "$WAIT_TIMEOUT" "$@"
    ;;
  status|ps)
    preflight
    "${COMPOSE[@]}" ps -a --format 'table {{.Service}}\t{{.State}}\t{{.Status}}\t{{.Ports}}'
    check_ollama
    ;;
  logs)
    preflight
    "${COMPOSE[@]}" logs -f --tail=100 "$@"
    ;;
  infra)
    preflight
    "${COMPOSE[@]}" up -d --wait --wait-timeout "$WAIT_TIMEOUT" "${INFRA[@]}"
    ;;
  seed)
    preflight
    "${COMPOSE[@]}" exec identity-service python -m identity_service.seed
    ;;
  shell)
    preflight
    need_service "${1:-}" shell
    "${COMPOSE[@]}" exec "$1" sh
    ;;
  clean)
    preflight
    warn "This removes all containers AND the Postgres volume (users, settings, audit log, cases)."
    read -r -p "Type 'delete' to continue: " answer
    [[ "$answer" == "delete" ]] || die "aborted"
    "${COMPOSE[@]}" down -v --remove-orphans
    ;;
  help|-h|--help)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    ;;
  *)
    die "unknown command '$cmd' (try: $0 help)"
    ;;
esac
