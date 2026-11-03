"""Circuit breaker: CLOSED → (fail_max failures) → OPEN → (reset_timeout) → HALF_OPEN → CLOSED | OPEN.

State lives in a `BreakerStateStore` so replicas can share it; the half-open probe limit is enforced per
process. Implemented natively on asyncio rather than wrapping pybreaker, whose async path needs Tornado.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import TypeVar

import structlog

from .clock import Clock, MonotonicClock
from .config import BreakerConfig
from .errors import CircuitOpenError
from .listeners import BreakerListener
from .policy import FailurePolicy, default_is_failure
from .store import BreakerSnapshot, BreakerStateStore, CircuitState, InMemoryStateStore

T = TypeVar("T")
log = structlog.get_logger(__name__)


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        config: BreakerConfig | None = None,
        *,
        store: BreakerStateStore | None = None,
        clock: Clock | None = None,
        listeners: Sequence[BreakerListener] = (),
        is_failure: FailurePolicy = default_is_failure,
    ) -> None:
        self.name = name
        self._config = config or BreakerConfig()
        self._store = store or InMemoryStateStore()
        self._clock = clock or MonotonicClock()
        # Kept by reference: the registry shares one list, so observers added later also see this breaker.
        self._listeners = listeners
        self._is_failure = is_failure
        self._lock = asyncio.Lock()
        self._probes_in_flight = 0

    async def state(self) -> CircuitState:
        """Current state, reporting OPEN as HALF_OPEN once the reset timeout has elapsed."""
        snap = await self._store.load(self.name)
        if snap.state is CircuitState.OPEN and self._reset_elapsed(snap):
            return CircuitState.HALF_OPEN
        return snap.state

    async def retry_after(self) -> float | None:
        """Seconds until an open circuit lets the next call through; None when calls are allowed."""
        snap = await self._store.load(self.name)
        if snap.state is CircuitState.OPEN and not self._reset_elapsed(snap):
            return self._retry_after(snap)
        return None

    async def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        is_probe = await self._before_call()
        try:
            result = await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._is_failure(exc):
                await self._record_failure()
            else:
                await self._record_success()
            raise
        else:
            await self._record_success()
            return result
        finally:
            if is_probe:
                self._probes_in_flight -= 1

    async def _before_call(self) -> bool:
        async with self._lock:
            snap = await self._store.load(self.name)
            if snap.state is CircuitState.OPEN:
                if not self._reset_elapsed(snap):
                    raise CircuitOpenError(self.name, self._retry_after(snap))
                snap = await self._transition(snap, CircuitState.HALF_OPEN)
            if snap.state is CircuitState.HALF_OPEN:
                if self._probes_in_flight >= self._config.half_open_max_calls:
                    raise CircuitOpenError(self.name, 0.0)
                self._probes_in_flight += 1
                return True
            return False

    async def _record_failure(self) -> None:
        async with self._lock:
            snap = await self._store.load(self.name)
            if snap.state is CircuitState.HALF_OPEN:
                await self._open(snap)
            elif snap.state is CircuitState.CLOSED:
                failures = snap.failures + 1
                if failures >= self._config.fail_max:
                    await self._open(replace(snap, failures=failures))
                else:
                    await self._store.save(self.name, replace(snap, failures=failures))

    async def _record_success(self) -> None:
        async with self._lock:
            snap = await self._store.load(self.name)
            if snap.state is CircuitState.HALF_OPEN:
                await self._transition(BreakerSnapshot(), CircuitState.CLOSED, old=snap.state)
            elif snap.state is CircuitState.CLOSED and snap.failures:
                await self._store.save(self.name, BreakerSnapshot())

    async def _open(self, snap: BreakerSnapshot) -> None:
        await self._transition(replace(snap, opened_at=self._clock.now()), CircuitState.OPEN, old=snap.state)

    async def _transition(
        self, snap: BreakerSnapshot, new: CircuitState, *, old: CircuitState | None = None
    ) -> BreakerSnapshot:
        previous = old or snap.state
        updated = replace(snap, state=new)
        await self._store.save(self.name, updated)
        if previous is not new:
            await self._notify(previous, new)
        return updated

    async def _notify(self, old: CircuitState, new: CircuitState) -> None:
        for listener in self._listeners:
            try:
                await listener.on_state_change(self.name, old, new)
            except Exception:  # a broken observer must never break the call path
                log.exception("breaker_listener_failed", dependency=self.name)

    def _reset_elapsed(self, snap: BreakerSnapshot) -> bool:
        return self._clock.now() - snap.opened_at >= self._config.reset_timeout

    def _retry_after(self, snap: BreakerSnapshot) -> float:
        return max(0.0, self._config.reset_timeout - (self._clock.now() - snap.opened_at))
