COMPOSE := docker compose -f deploy/docker-compose.yml --env-file .env

.PHONY: install lint format test infra-up infra-down up down logs

install:
	uv sync --all-packages
	@if [ -f frontend/package.json ]; then cd frontend && npm install; fi

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy libs/opspilot-common/src services/identity-service/src services/api-gateway/src services/settings-service/src services/mail-service/src services/privacy-service/src services/audit-service/src \
		services/approval-service/src services/jira-connector/src services/agent-orchestrator/src \
		services/reporting-service/src
	uv run lint-imports

format:
	uv run ruff format .
	uv run ruff check --fix .

PACKAGES := libs/opspilot-common $(wildcard services/*)

# Each package runs in its own pytest process: every service has its own `tests` package.
test:
	@set -e; for pkg in $(PACKAGES); do echo "== $$pkg"; uv run pytest $$pkg -q; done

infra-up:
	$(COMPOSE) up -d postgres redis rabbitmq toxiproxy jaeger

infra-down:
	$(COMPOSE) stop postgres redis rabbitmq toxiproxy jaeger

up:
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100
