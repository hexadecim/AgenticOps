# ADR-0002: Circuit breaker on every outbound call

- Status: Accepted · 2026-10-08

## Context
OpsPilot depends on a local LLM on a PC, Jira Cloud, external mail providers and its own services. Any
can be slow or down; without isolation, one stalled dependency exhausts workers and cascades.

## Decision
`opspilot_common.resilience.Resilient` wraps every outbound call with, outermost first:
**bulkhead → circuit breaker → retry (transient only) → per-attempt timeout**. Settings per dependency in
`deploy/resilience.yaml`.

- Implemented natively on asyncio (pybreaker's async support requires Tornado). State in Redis
  (`RedisStateStore` + `WallClock`) so all replicas share one circuit.
- Failures = timeouts, transport errors, 5xx. 4xx never trip the breaker and are never retried.
- Non-idempotent calls are retried only with an idempotency key.
- Transitions are observed by listeners: logs, Prometheus `opspilot_circuit_state`, and the
  `breaker.state_changed` event that drives the dashboard's dependency-health panel.

## Fallbacks
| Dependency open | Behaviour |
|---|---|
| ollama | Run parked as `waiting_llm`; resumed when half-open. Queue shows "AI paused". |
| jira | Approval recorded; create stays in jira-connector outbox, replayed with idempotency key. UI: "ticket creation pending". |
| mail-provider | Poll cycle skipped, "Mailbox polled N min ago" turns amber; outbound sends stay queued. |
| ops-service | Fix gate shows "Evidence unavailable"; Approve disabled. Never run a fix without fresh evidence. |
| privacy-service | **Fail closed**: processing stops; raw text never reaches the LLM. |
| rabbitmq | Outbox rows stay pending; relay retries. No event is lost. |
| any service (gateway) | 503 with structured error; SPA shows a non-blocking banner. |

## Consequences
+ Bounded latency and no cascades; explicit, tested degraded modes.
− Thresholds need tuning against the real PC's LLM latency (`ollama.timeout: 180`).
