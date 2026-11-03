# ADR-0001: Microservices with hexagonal services and database-per-service

- Status: Accepted · 2026-10-08

## Context
OpsPilot combines very different workloads: mailbox polling, slow local LLM inference (deepseek-r1:8b),
human approval queues, compliance-grade audit and GDPR processing. They fail, scale and change at
different rates, and several handle personal data that must be isolated.

## Decision
- Split into 11 services (see CLAUDE.md §2), each owning one Postgres schema with its own DB role and no
  grants on other schemas.
- Each service uses a hexagonal layout (domain → application → ports ← adapters) with a single
  composition root, enforced by import-linter.
- Sync HTTP only where an answer is needed immediately; everything else is events over RabbitMQ with a
  transactional outbox and idempotent inbox.
- The SPA talks only to `api-gateway` (BFF), which verifies the session JWT. identity-service issues JWTs
  (HS256 shared secret in dev; RS256 with public-key-only verifiers in production).

## Consequences
+ Faults are contained (an Ollama stall cannot block approvals or audit); personal data stays in
  privacy-service; services can be replaced independently (SOLID's open/closed at system level).
− More moving parts: needs the shared `opspilot-common` library, contract tests and tracing to stay sane.
− Eventual consistency between services (dashboard/audit may lag by seconds).
