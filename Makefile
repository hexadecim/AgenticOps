COMPOSE := docker compose -f deploy/docker-compose.yml --env-file .env

.PHONY: prod-secrets prod-up prod-down install lint format test test-system test-load test-postgres test-infra-up test-infra-down infra-up infra-down up down logs

install:
	uv sync --all-packages
	@if [ -f frontend/package.json ]; then cd frontend && npm install; fi

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy libs/opspilot-common/src services/identity-service/src services/api-gateway/src services/settings-service/src services/mail-service/src services/privacy-service/src services/audit-service/src \
		services/approval-service/src services/jira-connector/src services/agent-orchestrator/src \
		services/reporting-service/src services/ops-service/src
	uv run lint-imports

format:
	uv run ruff format .
	uv run ruff check --fix .

PACKAGES := libs/opspilot-common $(wildcard services/*)

# Each package runs in its own pytest process: every service has its own `tests` package.
test:
	@set -e; for pkg in $(PACKAGES); do echo "== $$pkg"; uv run pytest $$pkg -q; done

TEST_COMPOSE := docker compose -f deploy/docker-compose.test.yml

test-infra-up:
	$(TEST_COMPOSE) up -d --wait

test-infra-down:
	$(TEST_COMPOSE) down -v

# Real Postgres, RabbitMQ, Redis and Toxiproxy (test-infra-up first; never the dev stack).
test-system:
	uv run pytest tests/system -q

# Poller and approval queue at volume; prints the numbers (not part of CI: it takes minutes).
test-load:
	uv run pytest tests/load -q -s

# Every package's own tests again, on Postgres instead of SQLite (tests/system/postgres_plugin.py).
test-postgres:
	@set -e; for pkg in $(PACKAGES); do echo "== $$pkg"; PYTHONPATH=tests/system uv run pytest $$pkg -q -p postgres_plugin; done

infra-up:
	$(COMPOSE) up -d postgres redis rabbitmq toxiproxy jaeger

infra-down:
	$(COMPOSE) stop postgres redis rabbitmq toxiproxy jaeger

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

# Production: `make prod-secrets`, then `make prod-up`. deploy/prod/compose.env only satisfies the base file's
# required variables; the real values come from deploy/prod/secrets (see ADR-0009).
PROD_COMPOSE := docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.prod.yml --env-file deploy/prod/compose.env

prod-secrets:
	deploy/prod/generate-secrets.sh

prod-up:
	$(PROD_COMPOSE) up -d --build

prod-down:
	$(PROD_COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100
