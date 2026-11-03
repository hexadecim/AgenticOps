"""Publishes `breaker.state_changed` so reporting can show dependency health on the dashboard.

Registered by `EventRuntime` for every service. Publishing happens in the background and only once the bus is
connected: observing a breaker must never slow down or break the call that tripped it.
"""

from __future__ import annotations

import asyncio

import structlog

from ..resilience import CircuitState
from .catalogue import BreakerStateChanged
from .envelope import EventEnvelope
from .ports import EventPublisher

log = structlog.get_logger(__name__)


class BreakerEventListener:
    def __init__(self, service: str, publisher: EventPublisher, ready: asyncio.Event | None = None) -> None:
        self._service = service
        self._publisher = publisher
        self._ready = ready
        self._pending: set[asyncio.Task[None]] = set()

    async def on_state_change(self, dependency: str, old: CircuitState, new: CircuitState) -> None:
        if dependency == "rabbitmq":
            return  # the bus itself is the failing dependency; logging/metrics cover it
        if self._ready is not None and not self._ready.is_set():
            log.info("breaker_event_not_sent_bus_down", dependency=dependency, new=new.value)
            return
        payload = BreakerStateChanged(
            service=self._service, dependency=dependency, old=old.value, new=new.value
        )
        task = asyncio.create_task(self._send(EventEnvelope.wrap(payload, source=self._service)))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _send(self, envelope: EventEnvelope) -> None:
        try:
            await self._publisher.publish(envelope)
        except Exception:  # best effort: the state is also in Redis and in the metrics
            log.warning("breaker_event_not_sent", dependency=envelope.payload.get("dependency"))

    async def drain(self) -> None:
        """Wait for events still being sent (tests, shutdown)."""
        if self._pending:
            await asyncio.gather(*self._pending, return_exceptions=True)
