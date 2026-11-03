"""Per-process event runtime: broker connection, outbox relays and consumers.

A service must keep working while RabbitMQ is down: `start()` returns immediately and connects in the
background with backoff; outbox rows simply wait until the relay can publish them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Sequence

import structlog
from sqlalchemy import Table
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..resilience import ResilienceRegistry
from .breaker_listener import BreakerEventListener
from .outbox import OutboxRelay
from .ports import EventHandler
from .rabbitmq import RabbitConnection, RabbitConsumer, RabbitPublisher

log = structlog.get_logger(__name__)


class EventRuntime:
    def __init__(
        self,
        service: str,
        amqp_url: str,
        registry: ResilienceRegistry,
        *,
        relay_interval: float = 1.0,
        max_backoff: float = 30.0,
    ) -> None:
        self.service = service
        self._connection = RabbitConnection(amqp_url)
        self.publisher = RabbitPublisher(self._connection, registry.get("rabbitmq"))
        self._relays: list[OutboxRelay] = []
        self._consumers: list[tuple[str, Sequence[str], EventHandler]] = []
        self._relay_interval = relay_interval
        self._max_backoff = max_backoff
        self._task: asyncio.Task[None] | None = None
        self.connected = asyncio.Event()
        # Every service reports its circuit changes (dashboard: dependency health).
        self.breaker_events = BreakerEventListener(service, self.publisher, self.connected)
        registry.add_listener(self.breaker_events)

    def add_outbox(self, session_factory: async_sessionmaker[AsyncSession], table: Table) -> None:
        self._relays.append(OutboxRelay(session_factory, table, self.publisher))

    def add_consumer(self, purpose: str, bindings: Sequence[str], handler: EventHandler) -> None:
        self._consumers.append((f"{self.service}.{purpose}", bindings, handler))

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name=f"{self.service}-events")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        with contextlib.suppress(Exception):
            await self._connection.close()

    async def _run(self) -> None:
        await self._connect_with_backoff()
        for queue, bindings, handler in self._consumers:
            await RabbitConsumer(self._connection, queue, bindings, handler).start()
        while True:
            for relay in self._relays:
                try:
                    await relay.run_once()
                except Exception:
                    log.exception("outbox_relay_failed", service=self.service)
            await asyncio.sleep(self._relay_interval)

    async def _connect_with_backoff(self) -> None:
        delay = 1.0
        while True:
            try:
                await self._connection.open()
                self.connected.set()
                log.info("event_bus_connected", service=self.service)
                return
            except Exception as exc:
                log.warning("event_bus_unavailable", service=self.service, retry_in=delay, error=str(exc))
                await asyncio.sleep(delay)
                delay = min(delay * 2, self._max_backoff)
