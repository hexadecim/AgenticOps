"""`Resilient` composes the policies for one dependency.

Order, outermost first: bulkhead → circuit breaker → retry (transient errors only) → per-attempt timeout.
The breaker therefore counts one failure per logical call, after its retries are exhausted.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import TypeVar

from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from .breaker import CircuitBreaker
from .bulkhead import Bulkhead
from .clock import Clock
from .config import ResilienceConfig, ResilienceSettings
from .errors import DependencyTimeoutError, ResilienceError
from .listeners import BreakerListener, LoggingListener
from .policy import FailurePolicy, default_is_failure, default_is_transient
from .store import BreakerStateStore, CircuitState, InMemoryStateStore

T = TypeVar("T")


class Resilient:
    def __init__(
        self,
        name: str,
        config: ResilienceConfig,
        breaker: CircuitBreaker,
        bulkhead: Bulkhead,
        *,
        is_transient: FailurePolicy = default_is_transient,
    ) -> None:
        self.name = name
        self.config = config
        self.breaker = breaker
        self._bulkhead = bulkhead
        self._is_transient = is_transient

    async def call(self, fn: Callable[[], Awaitable[T]], *, retry: bool = True) -> T:
        """Run `fn` under all policies. Pass `retry=False` for non-idempotent operations."""
        async with self._bulkhead:
            return await self.breaker.call(lambda: self._attempts(fn, retry))

    async def call_with_fallback(
        self,
        fn: Callable[[], Awaitable[T]],
        fallback: Callable[[ResilienceError], Awaitable[T]],
        *,
        retry: bool = True,
    ) -> T:
        """Like `call`, but resilience-layer failures (open circuit, timeout, …) go to `fallback`."""
        try:
            return await self.call(fn, retry=retry)
        except ResilienceError as exc:
            return await fallback(exc)

    async def _attempts(self, fn: Callable[[], Awaitable[T]], retry: bool) -> T:
        policy = self.config.retry
        retrying = AsyncRetrying(
            stop=stop_after_attempt(policy.attempts if retry else 1),
            wait=wait_exponential_jitter(multiplier=policy.base_delay, max=policy.max_delay),
            retry=retry_if_exception(self._is_transient),
            reraise=True,
        )
        async for attempt in retrying:
            with attempt:
                return await self._with_timeout(fn)
        raise AssertionError("unreachable: tenacity reraises on exhaustion")  # pragma: no cover

    async def _with_timeout(self, fn: Callable[[], Awaitable[T]]) -> T:
        try:
            async with asyncio.timeout(self.config.timeout):
                return await fn()
        except TimeoutError as exc:
            raise DependencyTimeoutError(self.name, self.config.timeout) from exc


class ResilienceRegistry:
    """Builds and caches one `Resilient` per dependency name; feeds /health/ready."""

    def __init__(
        self,
        settings: ResilienceSettings | None = None,
        *,
        store: BreakerStateStore | None = None,
        clock: Clock | None = None,
        listeners: Sequence[BreakerListener] = (LoggingListener(),),
        is_failure: FailurePolicy = default_is_failure,
    ) -> None:
        self._settings = settings or ResilienceSettings()
        self._store = store or InMemoryStateStore()
        self._clock = clock
        self._listeners: list[BreakerListener] = list(listeners)  # shared with every breaker built here
        self._is_failure = is_failure
        self._items: dict[str, Resilient] = {}

    def get(self, name: str) -> Resilient:
        if name not in self._items:
            cfg = self._settings.for_dependency(name)
            breaker = CircuitBreaker(
                name,
                cfg.breaker,
                store=self._store,
                clock=self._clock,
                listeners=self._listeners,
                is_failure=self._is_failure,
            )
            self._items[name] = Resilient(name, cfg, breaker, Bulkhead(name, cfg.bulkhead, cfg.bulkhead_wait))
        return self._items[name]

    def add_listener(self, listener: BreakerListener) -> None:
        """Register an observer for every breaker of this registry, including ones that already exist."""
        self._listeners.append(listener)

    async def states(self) -> dict[str, CircuitState]:
        return {name: await item.breaker.state() for name, item in self._items.items()}
