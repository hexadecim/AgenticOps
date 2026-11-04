"""Breaker state storage. Redis lets every replica of a service share one circuit."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger(__name__)


class CircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass(frozen=True, slots=True)
class BreakerSnapshot:
    state: CircuitState = CircuitState.CLOSED
    failures: int = 0
    opened_at: float = 0.0


class BreakerStateStore(Protocol):
    async def load(self, name: str) -> BreakerSnapshot: ...
    async def save(self, name: str, snapshot: BreakerSnapshot) -> None: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self._data: dict[str, BreakerSnapshot] = {}

    async def load(self, name: str) -> BreakerSnapshot:
        return self._data.get(name, BreakerSnapshot())

    async def save(self, name: str, snapshot: BreakerSnapshot) -> None:
        self._data[name] = snapshot


class RedisStateStore:
    """Stores snapshots as JSON under `opspilot:breaker:<name>`. Pair with `WallClock`.

    Redis only shares the state between replicas, so losing it must not stop any call: this process then keeps
    the state itself, and stops asking Redis for `retry_after` seconds so that calls are not slowed by it."""

    def __init__(
        self,
        redis: Redis,
        prefix: str = "opspilot:breaker:",
        *,
        timeout: float = 0.25,
        retry_after: float = 5.0,
    ) -> None:
        self._redis = redis
        self._prefix = prefix
        self._timeout, self._retry_after = timeout, retry_after
        self._local = InMemoryStateStore()
        self._skip_until = 0.0

    def _available(self) -> bool:
        return time.monotonic() >= self._skip_until

    def _lost(self, exc: Exception) -> None:
        if self._skip_until == 0.0:
            log.warning("breaker_store_unavailable", error=type(exc).__name__)
        self._skip_until = time.monotonic() + self._retry_after

    def _regained(self) -> None:
        if self._skip_until != 0.0:
            log.info("breaker_store_recovered")
        self._skip_until = 0.0

    async def load(self, name: str) -> BreakerSnapshot:
        if not self._available():
            return await self._local.load(name)
        try:
            async with asyncio.timeout(self._timeout):
                raw = await self._redis.get(self._prefix + name)
        except (RedisError, OSError, TimeoutError) as exc:
            self._lost(exc)
            return await self._local.load(name)
        self._regained()
        if raw is None:
            return BreakerSnapshot()
        data = json.loads(raw)
        snapshot = BreakerSnapshot(
            state=CircuitState(data["state"]),
            failures=int(data["failures"]),
            opened_at=float(data["opened_at"]),
        )
        await self._local.save(name, snapshot)
        return snapshot

    async def save(self, name: str, snapshot: BreakerSnapshot) -> None:
        await self._local.save(name, snapshot)
        if not self._available():
            return
        try:
            async with asyncio.timeout(self._timeout):
                await self._redis.set(self._prefix + name, json.dumps(asdict(snapshot)))
        except (RedisError, OSError, TimeoutError) as exc:
            self._lost(exc)
