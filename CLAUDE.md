# CLAUDE.md — OpsPilot

Instructions and long-lived memory for anyone (human or Claude) working in this repo.
Read this file fully before changing code. Update the **Project memory log** at the end of every milestone.

## 1. What OpsPilot is

An agentic support console. Agents read a mailbox, pseudonymise and classify each email, and *propose*
actions; a named human approves every action before anything leaves the system, touches production or
reaches a customer.

- Wireframe (source of truth for UI): https://claude.ai/artifact/9MVMvgd7L4LeEs1yPFUbV7
- Screens: Login (+TOTP) · Approvals queue · Approve ticket (T1) · Approve fix (T2) · Approve message (T3) ·
  Dashboard · Audit log · Privacy/GDPR · Admin (not in wireframe; same visual language)
- LLM: `deepseek-r1:8b` on the host's Ollama (`http://localhost:11434`, from containers
  `http://host.docker.internal:11434`). Embeddings: `nomic-embed-text`.
- Ticketing: Jira Cloud (REST v3). Mailbox: IMAP | Gmail API | Microsoft Graph, chosen in Admin.
- Full plan: `docs/architecture.md`; decisions: `docs/adr/`.
- **Status tracker: `docs/implementation-plan.md`** — tick items there (and add to the memory log below)
  at the end of every work session; never mark an item done without having verified it.

## 2. Architecture memory

Microservices, one Postgres **schema per service** (never read another service's schema), RabbitMQ topic
exchange `opspilot.events` for state changes, synchronous HTTP only where an answer is needed now.

| Service | Owns | Publishes | Consumes |
|---|---|---|---|
| api-gateway | – | – | `*` (SSE fan-out) |
| identity-service | `identity` | `user.*` | – |
| settings-service | `settings` | `settings.changed` | – (port 8002) |
| mail-service | `mail` | `email.received`, `message.sent`, `privacy.recorded` | `settings.changed` (`message.send.requested` from M7) – (port 8003) |
| privacy-service | `privacy` | `privacy.recorded` | – (sync HTTP, port 8004) |
| agent-orchestrator | `orchestrator` | `approval.requested`, `run.*`, `agent.tool_called` | `email.received`, `approval.decided`, `jira.issue.*`, `settings.changed` (port 8008) |
| approval-service | `approval` | `approval.decided` (`approval.reminder` from M6) | `approval.requested`, `jira.issue.*` (port 8006) |
| jira-connector | `jira` | `jira.issue.created`, `jira.issue.linked`, `jira.issue.failed` | `approval.requested`, `approval.decided`, `settings.changed` (port 8007) |
| ops-service | `ops` | `probe.status`, `action.executed` | – (sync HTTP) |
| audit-service | `audit` | – | `#` (everything) (port 8005) |
| reporting-service | `reporting` | – | `#` (everything) (port 8009) |

- Event types and payload schemas: `libs/opspilot-common/src/opspilot_common/events/catalogue.py`
  (versioned; additive changes only, breaking change ⇒ new `vN`).
- Resilience settings per dependency: `deploy/resilience.yaml`.
- Approval gates and who may decide them: `opspilot_common/auth/roles.py` (`GATE_APPROVERS`).
- Service-to-service calls go to `/internal/v1/...` with a service token (`ServiceTokenIssuer(secret, "<service>")`,
  checked by `opspilot_common.web.service_dependency`). The gateway never routes `/internal`. Who may read what:
  settings `READERS` (settings-service catalogue), raw email `RAW_EMAIL_READERS` (mail-service bootstrap: only
  privacy-service), pseudonymise/detokenise/reveal rules in `privacy_service/domain/records.py`.
- Privacy design (detection, vault, fail closed, reveal, append-only audit): `docs/adr/0005-privacy-gate.md`.

## 3. Non-negotiable rules

1. **Hexagonal layout in every service**: `app/domain` (pure) → `app/application` (use cases) →
   `app/ports` (Protocols) ← `app/adapters` (inbound: routers, consumers; outbound: db, http, broker).
   `app/bootstrap.py` is the only composition root. `domain`/`application` never import `adapters`,
   FastAPI, SQLAlchemy, httpx or aio-pika. Enforced by `import-linter` (`.importlinter`).
2. **SOLID** — checklist for every change:
   - S: one use case per class; routers and consumers only translate and delegate.
   - O: new provider/integration = new adapter + registry entry; no edits to use cases.
   - L: every adapter of a port passes the port's shared contract test suite.
   - I: narrow ports (`MailReader` ≠ `MailSender`, `IssueCreator` ≠ `IssueSearcher`).
   - D: depend on ports; wire concretes in `bootstrap.py`; LangGraph nodes get ports via
     `config["configurable"]`, never by importing clients.
3. **Every outbound call** (other service, Ollama, Jira, mail provider, broker publish) goes through
   `opspilot_common.resilience` (`Resilient` / `ResilientHttpClient`). No bare `httpx` calls.
4. **Every state change** is written with an outbox row in the same transaction and is visible in the audit log.
5. **The LLM sees only tokenised text.** privacy-service down ⇒ fail closed, never fall back to raw email.
6. **No action without an approval event.** Ticket creation, production actions and outbound customer
   email happen only after `approval.decided` with `decision=approve` from an allowed role.
7. **Consumers are idempotent** (inbox table of processed event ids). Jira writes carry an idempotency key.
8. Audit log is append-only (DB trigger). Never add UPDATE/DELETE paths to it.
9. Secrets are Fernet-encrypted at rest (settings-service) and never logged or put in events.

## 4. Conventions

- Python 3.12, `uv` workspace (root `pyproject.toml`), ruff (lint + format), mypy `--strict`, pytest +
  pytest-asyncio (auto mode), pydantic v2, async SQLAlchemy 2 + Alembic, structlog JSON logs with
  `correlation_id`.
- Packages: shared lib `opspilot_common`; each service is a uniquely named package (`identity_service`,
  `api_gateway`, …) under `services/<name>/src/` — never `app`, because all services share one uv venv.
- Frontend: React + Vite + TypeScript strict, TanStack Query, react-router; theme tokens copied from the
  wireframe into `frontend/src/theme/tokens.css` (IBM Plex Sans/Mono, light/dark/system).
- Tests: `tests/unit` (fakes for ports), `tests/contract` (port suites), `tests/integration` (real infra).
- Git: branches `feat/<milestone>-<topic>`, Conventional Commits (`feat(mail): …`).

## 5. Commands

```bash
./opspilot.sh up    # master script: build + start all, wait for health, print URLs (help: ./opspilot.sh help)
make install        # uv sync --all-packages + npm install
make up             # http://localhost:8080 (SPA) · :8000/api/docs (gateway) · :15672 (RabbitMQ)
make lint           # ruff + mypy + import-linter
make test           # all unit + contract tests
make infra-up       # postgres, redis, rabbitmq, toxiproxy, jaeger
make up / make down # whole stack via deploy/docker-compose.yml
uv run --package opspilot-common pytest libs/opspilot-common   # one package
curl -s localhost:11434/api/tags                                # is Ollama up?
```

## 6. Recipes

- **Add a mailbox provider**: implement `MailReader` + `MailSender` in
  `services/mail-service/src/mail_service/adapters/outbound/providers/<name>.py` (hand MIME to the shared
  `mime.parse_mime`; translate `ResilienceError` → `MailboxUnavailableError`, refusals → `MailboxRejectedError`),
  register it in `providers/__init__.py:default_registry`, add its fields (with `when`) and a connection tester in
  settings-service, and add a harness param to `tests/contract/test_mailbox_port.py` until it passes.
- **Add an event**: payload model + `EventType` member in `catalogue.py`, bump nothing for additive fields,
  add bindings in the consuming service's `bootstrap.py`, add a describer to
  `audit_service/domain/mapping.py:DESCRIBERS` (a test fails until you do). People are shown by role: carry
  `actor_role`, never a name.
- **Add an outbound dependency**: add a block to `deploy/resilience.yaml`, build the client via
  `ResilientHttpClient.from_config(name, …)`, define the fallback behaviour and document it in ADR-0002.
- **Add an approval gate**: new `Gate` member + `GATE_APPROVERS` entry, a graph node that calls
  `interrupt()`, an approval screen, audit mapping.
- **Add a service**: copy the hexagonal skeleton, add its schema to `deploy/postgres/init.sql`, its
  contract to `.importlinter`, its block to `docker-compose.yml`, its row to §2.

## 7. Project memory log

### Decisions & status
- 2026-10-08 — Plan approved: microservices + circuit breakers + SOLID, full wireframe scope, FastAPI +
  React, Ollama deepseek-r1:8b, Jira Cloud, admin-configurable mailbox (IMAP/Gmail/Graph).
- 2026-10-08 — Milestone 0 (foundations) done: repo, CLAUDE.md, ADRs 0001–0004, `opspilot-common`
  (native asyncio circuit breaker + Redis-shared state, retry, timeout, bulkhead, `ResilientHttpClient`;
  event envelope + versioned catalogue, outbox relay, inbox dedupe, RabbitMQ retry/DLQ topology; JWT +
  gate roles; structlog; FastAPI health/metrics helpers), infra compose, CI. 30 tests, ruff/mypy
  strict/import-linter green. Infra verified live (`scripts/demo_foundations.py`): outbox→RabbitMQ→consumer,
  retry→DLQ, Ollama breaker open/half-open/closed via toxiproxy with Redis-shared state, deepseek JSON intake.
- 2026-10-08 — Milestone 1a (auth) done: identity-service (argon2id, TOTP enrolment + replay guard,
  lockout 5/15 min, forced password change, sliding 30-min JWT, admin user CRUD, last-admin/self-lockout
  guards, outbox events), api-gateway (HttpOnly SameSite=Strict cookie, CSRF header, Redis rate limit,
  503 on open circuit), React shell + Sign-in + Users and roles, Docker images, nginx. 58 tests.
  Live: http://localhost:8080. Demo users seeded via `python -m identity_service.seed` (in container).
- 2026-10-08 — Milestone 1b done: settings-service (field-spec catalogue, encrypted write-only secrets,
  optimistic locking, `settings.changed` with names only, connection testers for Ollama/IMAP+SMTP/Jira/
  Gmail/Graph on `probe-*` circuits), gateway settings + System health routes, all services on
  Redis-shared breakers, rate-limit IP fix, Admin › Integrations + System health screens, GreenMail dev
  mailbox. 81 tests. Mailbox (dev mailbox) and LLM sections are saved as version 1 in the dev database.
- 2026-10-08 — Milestone 2 done: service tokens (derived key, own iss/aud) + settings-service internal resolved
  API with per-section reader allow-list; mail-service (hexagonal, schema `mail`, :8003): `MailReader`/`MailSender`
  ports, provider registry, IMAP+SMTP / Gmail / Graph adapters on the `mail-provider` circuit, one shared MIME
  parser, contract suite over 5 adapters incl. live GreenMail, poller with batches + 24 h look-back, Message-ID
  dedupe, threading, 30-day retention job, hot reload on `settings.changed`, ids-only `email.received`, internal
  email API, status with amber states; gateway `/api/mail/status`; drawer status row. 134 tests. Live: SMTP mail →
  one `email.received` in 37 s; broken host → breaker open → amber → restored → recovered; no duplicates after a
  read-position reset. Gmail/Graph verified against stubs only.
- 2026-10-08 — Milestone 3 done: privacy-service (only reader of raw mail; header identities + patterns +
  Presidio/spaCy small EN/DE; signature/quote stripping; stable tokens in a Fernet/HMAC vault; leak check; fail
  closed; audited 5-min reveal; detokenise for mail-service only) and audit-service (consumes `#`, all 16 events
  mapped, people by role, append-only via triggers, tabs/search/keyset paging); gateway routes; ADR-0005.
  173 tests. Live: sign-in in audit API, UPDATE/DELETE/TRUNCATE rejected even for superuser, pseudonymise +
  reveal + Privacy tab, 503 with mail-service stopped. Fixed an M2 bug found on the user's real Gmail (NUL byte
  blocked the poller silently). Admin password reset at the user's request (not recorded here).
- 2026-10-08 — Milestone 4 (MVP) done: agent-orchestrator (LangGraph + AsyncPostgresSaver; privacy_gate → intake
  → dedupe → request_ticket_approval → gate_ticket; worker-only execution; waiting_llm / waiting_privacy parking),
  approval-service (gate roles, reasons, idempotent decisions, ticket outcome), jira-connector (job outbox, label
  replay guard, ADF, link-by-comment, `jira.issue.failed`), gateway SSE + approval routes, Approvals and Approve
  ticket screens. 204 tests. Live: two test emails approved by the user in the UI → KAN-18, KAN-19 in Jira; audit
  chain complete; Ollama fallback seen live and fixed (ADR-0004 amendment).
- 2026-10-08 — Milestone 5 done: breaker events actually wired (EventRuntime registers the listener; shared listener
  list), reporting-service read models (cases, stages, service levels, incident timelines, dependency health) built
  only from events, gateway dashboard routes, Dashboard and Audit log screens. 212 tests. Live: backfill by outbox
  replay (not mail) → KAN-19 timeline and SLA breach shown; mail-service breaker trip and recovery on the dashboard.
- Next: Milestone 6 — ops-service (probes, masked logs, runbook RAG, allow-listed actions), diagnostics node, T2 gate.

### Gotchas
- deepseek-r1 emits `<think>…</think>`; use `ChatOllama(reasoning=True)` so reasoning lands in
  `additional_kwargs["reasoning_content"]`, and request structured output with `method="json_schema"`.
  Do not rely on free tool-calling with the 8B model.
- Jira Cloud v3 issue `description` must be Atlassian Document Format (ADF), not plain text.
- Circuit breaker counts only 5xx, timeouts and transport errors; 4xx are caller errors and never trip it.
- Containers reach host Ollama via `host.docker.internal` (compose sets `extra_hosts`).
- uv workspace `members` lists `libs/*` only; add each `services/<name>` explicitly when it gets a
  `pyproject.toml` (a glob matching a folder without one breaks `uv sync`).
- deepseek-r1:8b with thinking takes ~70 s per intake call on this PC (5–6k reasoning chars); keep
  `ollama.timeout` ≥ 180 s and treat LLM steps as async background work, never inside an HTTP request.
- Run compose from repo root with `--env-file .env` (or use `make`); zsh does not word-split `$VAR` commands.
- Router modules that build `Annotated[..., Depends(x)]` aliases inside a factory must NOT use
  `from __future__ import annotations` (FastAPI then sees a string it cannot resolve → 422 query param).
- `ResilientHttpClient` maps httpx transport errors to `DependencyUnreachableError`/`DependencyTimeoutError`:
  catch `ResilienceError` only, never raw httpx exceptions.
- Password policy rejects passwords containing the email name (≥4 chars) — bootstrap/demo passwords too.
- Domain/application code imports `opspilot_common.auth.roles` / `.principal` directly, not
  `opspilot_common.auth` (its `__init__` pulls in PyJWT; import-linter forbids that in the core).
- Each service has its own `tests` package, so `make test` runs one pytest process per package.
- Never enrol the real admin's TOTP during automated checks; use a throwaway admin and deactivate it.
- Dev mailbox: GreenMail `devmail` (IMAP 3143 / SMTP 3025, no TLS), user `support@opspilot.local`,
  password `support-dev-pass`. From the host: localhost:3143 / localhost:3025.
- Scripted `str.replace` edits silently miss after `ruff format` rewraps lines: assert the old text exists
  (or use the Edit tool) — this bit three times in milestone 1.
- Breakers share the registry's listener list, so `add_listener` works at any time. `EventRuntime` already adds
  the `breaker.state_changed` publisher for every service: don't add another one.
- A domain/application module must not import `opspilot_common.resilience` (it pulls in httpx; import-linter
  checks indirect imports). Adapters translate `ResilienceError` into the service's own domain errors.
- GreenMail creates a user only on first SMTP delivery (password = address) and keeps mail in memory: restarting
  `devmail` (e.g. `make up` recreating it) empties the dev mailbox. It also answers `UID N:*` with nothing when N
  is past the end, unlike RFC 3501 (`*` = highest UID); the IMAP adapter filters `uid > last` either way.
- A breaker opened by a bad mailbox config stays open for its `reset_timeout` (120 s for `mail-provider`) even
  after the admin fixes the settings; the status says "next try in N s".
- Never let a script write to or empty a service's real `outbox` table; demo/test tables get their own names.
- Real mail contains NUL and other control characters; Postgres TEXT rejects NUL. Clean every parsed text field
  (`mime.clean`), and never let one bad message fail silently: record it as a failed poll.
- The user may point the mailbox at their real Gmail. For live tests use synthetic mail already stored, or ask
  before switching settings; never print real mail content in checks (counts only).
- Domain code imports `opspilot_common.events.catalogue` as a module (`import ….catalogue as c`), not
  `from opspilot_common.events import …` (the package `__init__` pulls in aio-pika and SQLAlchemy).
- Presidio needs `[tool.hatch.metadata] allow-direct-references = true` for the spaCy model wheel URLs, and
  loads ~1 s at start-up; build it once in bootstrap. Its PERSON results include greetings ("Hallo Support"):
  always pass NER output through `clean_people`.
- deepseek-r1:8b at temperature 0 loops inside JSON fields until the timeout. Keep temperature 0.6, bound every
  string/list in LLM schemas with `max_length` (Ollama turns it into the grammar), cap `num_predict`.
- LangGraph re-runs an interrupted node from its start on resume: put side effects (publishing approval requests)
  in a separate node before the `interrupt()` node; make ids deterministic (`approval_id_for`).
- Ollama serves one request at a time: a manual LLM test while the orchestrator is working queues behind it
  (stop agent-orchestrator for clean measurements; it re-queues interrupted runs at start-up).
- `ruff --fix` may strip `as exc` it thinks unused when only a lambda uses it: bind `message = exc.message` first.
- No email may be sent to the user's Gmail for tests (their instruction). For end-to-end tests store a synthetic
  email in mail-service and publish `email.received`; approvals are done by the user as analyst@opspilot.local.
- New read-model consumers miss history. Backfill by re-publishing outboxes (`update <schema>.outbox set
  published_at = null`): every consumer is inbox-guarded so repeats are ignored. Replay mail-service's outbox
  only for specific emails the user asked to triage (it was done for the user's 08 Oct Gmail); a blanket replay
  would triage all old real mail.
- deepseek with reasoning can spend the whole 2048-token budget thinking on long emails and return no JSON; the
  intake adapter retries once without reasoning. Phrases like "None reported" must normalise to null.
- Tests written with `# fmt: skip` on long lines fail E501; let ruff format wrap instead.
- Live checks as a person: mint a session JWT inside a container that has `JWT_SECRET` and call the gateway
  (`localhost:8000` from api-gateway, `api-gateway:8000` from others); for a real sign-in create a throwaway
  user, complete TOTP with `pyotp`, then deactivate it.
