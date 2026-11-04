# ADR-0009: Production hardening

Status: accepted (2026-11-04)

## Context

Up to milestone 8 OpsPilot ran on development defaults: one shared secret for every token, dev database passwords, plain
HTTP, migrations at every container start. Tests ran on SQLite, so nothing checked Postgres-only behaviour or anything
concurrent. No trace connected what the services do for one case.

## Decision

**Secrets are files, one folder per service.** Pydantic reads settings from `SECRETS_DIR`, file name = field name, so a
service holds only what its folder holds. `deploy/prod/generate-secrets.sh` writes that layout. The contract is the
layout, not a product: Vault Agent, a Kubernetes CSI driver or similar can write the same files. Which manager to use
is the operator's decision and is not made here.

**RS256, with one key per service.** `opspilot_common.auth.AuthKeys` is the only place that chooses keys. Without
`AUTH_KEYS_DIR` it uses the development secret; with it, only identity-service holds the session signing key, and every
service signs its own service tokens. A service token is checked against the public key of the service it names, so a
compromised service cannot name another. A service with no usable key refuses to start.

**Roles.** Each schema's role has a random password. The audit schema belongs to `audit_owner`, which runs its migrations.
At run time `audit-service` has SELECT and INSERT only, so it cannot disable the append-only triggers (rule 8). A
database superuser still can: that needs operational control, not code.

**Migrations run once, as jobs.** `<service>-migrate` runs before each service; services get `RUN_MIGRATIONS=false`.
Development is unchanged.

**HTTPS at nginx.** `frontend/nginx.tls.conf` redirects HTTP, sends HSTS, and strips a client's `traceparent`. Only nginx
publishes ports. `generate-secrets.sh` makes a self-signed certificate so the stack starts; a real one replaces it.

**One optional limit on where settings point.** `ALLOWED_TARGET_HOSTS` restricts the URLs and host names an admin can
save or test. Empty means any host, because admins are trusted and development uses local names.

**Tracing is written by hand, not by instrumentation packages.** Spans carry names, ids and status codes: never URLs,
queries, SQL, exception messages or email text. A trace store has no erasure and no retention job, so personal data must
never reach it. FastAPI's own telemetry exports URL queries and exception messages, so every service turns it off
(`NO_NATIVE_TELEMETRY`; a test checks all of them). Third parties never receive trace ids.

**A case is one trace.** The W3C `traceparent` travels in the event envelope and is stored on queued work (runs, Jira jobs,
fix jobs, outgoing messages, approvals), so it survives the bus, a background worker and a wait for a person. Within a
trace the correlation id is the trace id, so an audit entry finds its trace. Before this, events from consumers and workers
had no correlation id at all.

**Concurrency is settled by the database.** Tests on real Postgres showed that three people deciding one approval at once
produced three `approval.decided` events, approve and reject together; and that five stores acknowledging one erasure lost
four of the answers, leaving it erasing forever. So:
- Reads that lead to a decision take a row lock: approvals, data requests, runs.
- A send is claimed with a conditional UPDATE, so one message goes out once with any number of replicas.
- Consumers take one event at a time per queue (`prefetch_count=1`), which keeps bus order and stops handlers interleaving.

**Losing Redis must not stop calls.** Breaker state lives in Redis to be shared between replicas. Chaos tests showed that
without Redis every outbound call in every service failed. `RedisStateStore` now keeps state locally while Redis is away and
stops asking it for 5 seconds at a time. The price: replicas do not share a circuit during the outage.

**Three levels of test on real infrastructure** (`deploy/docker-compose.test.yml`, own ports, memory-only database):
`make test-system` (migrations against the code's tables, chaos on every breaker, broker outage, races), `make test-postgres`
(every package's own tests on Postgres, unchanged), `make test-load` (poller and queue at volume, not in CI).

## Consequences

+ A stolen service container exposes that service's data only; sessions cannot be forged from any other service.
+ Regressions in migrations, locking and breakers fail in CI.
− One event at a time lowers a consumer's peak throughput, and a slow handler delays that queue. Handlers are short today
  (load test: 1,000 emails, 13/s through the poller on the reference PC).
− Key rotation is manual: replace the files and restart. Sessions end when the session key changes.
− The orchestrator, jira-connector and ops-service workers still assume one replica each. A second replica of these would
  run jobs twice (their results are idempotent by key, the model's work is not). Scaling them needs a claim like mail-service's.
− Redis has no password inside the compose network, and traffic between services is plain HTTP inside it. Acceptable on one
  host; not across hosts.
− Jaeger all-in-one keeps traces in memory, so they are lost on restart. It also has no sign-in, so it publishes no port in production.
