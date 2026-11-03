"""Observers notified when a circuit changes state (Observer pattern; open for extension)."""

from __future__ import annotations

from typing import Protocol

import structlog
from prometheus_client import Counter, Gauge

from .store import CircuitState

log = structlog.get_logger(__name__)

_STATE_VALUE = {CircuitState.CLOSED: 0, CircuitState.HALF_OPEN: 1, CircuitState.OPEN: 2}


class BreakerListener(Protocol):
    async def on_state_change(self, dependency: str, old: CircuitState, new: CircuitState) -> None: ...


class LoggingListener:
    async def on_state_change(self, dependency: str, old: CircuitState, new: CircuitState) -> None:
        level = log.warning if new is CircuitState.OPEN else log.info
        level("circuit_state_changed", dependency=dependency, old=old.value, new=new.value)


class PrometheusListener:
    """Exposes `opspilot_circuit_state{dependency}` (0 closed, 1 half-open, 2 open)."""

    _gauge = Gauge("opspilot_circuit_state", "Circuit breaker state", ["dependency"])
    _transitions = Counter(
        "opspilot_circuit_transitions_total", "Circuit breaker transitions", ["dependency", "to"]
    )

    async def on_state_change(self, dependency: str, old: CircuitState, new: CircuitState) -> None:
        self._gauge.labels(dependency=dependency).set(_STATE_VALUE[new])
        self._transitions.labels(dependency=dependency, to=new.value).inc()
