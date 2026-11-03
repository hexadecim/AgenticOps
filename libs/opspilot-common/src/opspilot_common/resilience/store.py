"""Breaker state storage. Redis lets every replica of a service share one circuit."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol

from redis.asyncio import Redis


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
    """Stores snapshots as JSON under `opspilot:breaker:<name>`. Pair with `WallClock`."""

    def __init__(self, redis: Redis, prefix: str = "opspilot:breaker:") -> None:
        self._redis = redis
        self._prefix = prefix

    async def load(self, name: str) -> BreakerSnapshot:
        raw = await self._redis.get(self._prefix + name)
        if raw is None:
            return BreakerSnapshot()
        data = json.loads(raw)
        return BreakerSnapshot(
            state=CircuitState(data["state"]),
            failures=int(data["failures"]),
            opened_at=float(data["opened_at"]),
        )

    async def save(self, name: str, snapshot: BreakerSnapshot) -> None:
        await self._redis.set(self._prefix + name, json.dumps(asdict(snapshot)))
