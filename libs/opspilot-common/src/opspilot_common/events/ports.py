"""Ports for publishing and handling events. Adapters: RabbitMQ, in-memory, outbox."""

from typing import Protocol

from .envelope import EventEnvelope


class EventPublisher(Protocol):
    async def publish(self, envelope: EventEnvelope) -> None: ...


class EventHandler(Protocol):
    async def __call__(self, envelope: EventEnvelope) -> None: ...
