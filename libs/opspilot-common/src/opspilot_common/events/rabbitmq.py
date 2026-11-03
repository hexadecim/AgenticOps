"""RabbitMQ adapters (aio-pika).

Topology per consumer queue `<service>.<name>`:
  opspilot.events (topic) ──binding──▶ <queue>
  handler error ─nack─▶ <queue>.retry (TTL, dead-letters back to <queue> via default exchange)
  after `max_retries` deliveries ──▶ <queue>.dlq (parked for inspection, never auto-consumed)
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import aio_pika
import structlog
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage, AbstractRobustConnection

from ..resilience import Resilient
from .envelope import EventEnvelope
from .ports import EventHandler

log = structlog.get_logger(__name__)

EXCHANGE = "opspilot.events"


class RabbitConnection:
    """Owns one robust connection + channel per service process."""

    def __init__(self, url: str) -> None:
        self._url = url
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None

    async def open(self) -> None:
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel(publisher_confirms=True)
        await self._channel.set_qos(prefetch_count=16)
        self._exchange = await self._channel.declare_exchange(
            EXCHANGE, aio_pika.ExchangeType.TOPIC, durable=True
        )

    @property
    def channel(self) -> AbstractChannel:
        if self._channel is None:
            raise RuntimeError("RabbitConnection.open() has not been awaited")
        return self._channel

    @property
    def exchange(self) -> AbstractExchange:
        if self._exchange is None:
            raise RuntimeError("RabbitConnection.open() has not been awaited")
        return self._exchange

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()


class RabbitPublisher:
    """EventPublisher adapter; every publish runs under the `rabbitmq` circuit breaker."""

    def __init__(self, connection: RabbitConnection, resilient: Resilient) -> None:
        self._connection = connection
        self._resilient = resilient

    async def publish(self, envelope: EventEnvelope) -> None:
        message = aio_pika.Message(
            body=envelope.to_bytes(),
            content_type="application/json",
            message_id=envelope.id,
            correlation_id=envelope.correlation_id,
            type=envelope.type,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        # Publishing is idempotent for consumers (inbox dedupes by event id), so retries are safe.
        await self._resilient.call(
            lambda: self._connection.exchange.publish(message, routing_key=envelope.type)
        )


class RabbitConsumer:
    def __init__(
        self,
        connection: RabbitConnection,
        queue: str,
        bindings: Sequence[str],
        handler: EventHandler,
        *,
        max_retries: int = 5,
        retry_delay_ms: int = 5_000,
    ) -> None:
        self._connection = connection
        self._queue_name = queue
        self._bindings = tuple(bindings)
        self._handler = handler
        self._max_retries = max_retries
        self._retry_delay_ms = retry_delay_ms

    async def start(self) -> None:
        channel = self._connection.channel
        retry_name, dlq_name = f"{self._queue_name}.retry", f"{self._queue_name}.dlq"
        await channel.declare_queue(dlq_name, durable=True)
        await channel.declare_queue(
            retry_name,
            durable=True,
            arguments={
                "x-message-ttl": self._retry_delay_ms,
                "x-dead-letter-exchange": "",
                "x-dead-letter-routing-key": self._queue_name,
            },
        )
        queue = await channel.declare_queue(
            self._queue_name,
            durable=True,
            arguments={"x-dead-letter-exchange": "", "x-dead-letter-routing-key": retry_name},
        )
        for pattern in self._bindings:
            await queue.bind(self._connection.exchange, routing_key=pattern)
        await queue.consume(self._on_message)
        log.info("consumer_started", queue=self._queue_name, bindings=list(self._bindings))

    async def _on_message(self, message: AbstractIncomingMessage) -> None:
        try:
            envelope = EventEnvelope.from_bytes(message.body)
        except ValueError:
            log.error("event_unparseable", queue=self._queue_name, message_id=message.message_id)
            await self._park(message)
            return
        try:
            await self._handler(envelope)
        except Exception:
            deliveries = _death_count(message.headers, self._queue_name) + 1
            if deliveries >= self._max_retries:
                log.exception("event_parked", event_id=envelope.id, type=envelope.type, deliveries=deliveries)
                await self._park(message)
            else:
                log.exception("event_retry", event_id=envelope.id, type=envelope.type, deliveries=deliveries)
                await message.reject(requeue=False)  # dead-letters to <queue>.retry
            return
        await message.ack()

    async def _park(self, message: AbstractIncomingMessage) -> None:
        await self._connection.channel.default_exchange.publish(
            aio_pika.Message(
                body=message.body,
                headers=message.headers,
                message_id=message.message_id,
                type=message.type,
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=f"{self._queue_name}.dlq",
        )
        await message.ack()


def _death_count(headers: dict[str, Any] | None, queue: str) -> int:
    for death in (headers or {}).get("x-death", []) or []:
        if isinstance(death, dict) and death.get("queue") == queue:
            return int(death.get("count", 0))
    return 0
