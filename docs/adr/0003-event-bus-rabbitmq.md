# ADR-0003: RabbitMQ topic exchange with outbox/inbox

- Status: Accepted · 2026-10-08

## Decision
- One durable topic exchange `opspilot.events`; routing key = event type (`approval.decided`).
- Producers write events to their schema's `outbox` table in the same transaction as the state change;
  an `OutboxRelay` publishes them in order (publisher confirms, under the `rabbitmq` breaker).
- Consumers own durable queues `<service>.<purpose>`, dedupe via an `inbox` table, retry through
  `<queue>.retry` (TTL dead-letter loop) and park poison messages in `<queue>.dlq` after 5 deliveries.
- Event payloads are versioned pydantic models in `events/catalogue.py` and carry tokens, never raw
  personal data, because audit and reporting consume everything.

## Alternatives
Kafka (heavier to run on one PC; ordering per partition not needed), Redis Streams (weaker DLQ/routing
tooling), NATS JetStream (good, but less familiar operational tooling).
