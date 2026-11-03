"""Reads every service's circuit state from Redis (all services share `opspilot:breaker:*`)."""

from __future__ import annotations

import json

from opspilot_common.resilience import BREAKER_KEY_PREFIX, BreakerSnapshot, CircuitState
from redis.asyncio import Redis


class RedisBreakerStateReader:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def snapshots(self) -> dict[str, BreakerSnapshot]:
        result: dict[str, BreakerSnapshot] = {}
        async for key in self._redis.scan_iter(match=f"{BREAKER_KEY_PREFIX}*", count=200):
            raw = await self._redis.get(key)
            if raw is None:
                continue
            data = json.loads(raw)
            name = (key.decode() if isinstance(key, bytes) else key).removeprefix(BREAKER_KEY_PREFIX)
            result[name] = BreakerSnapshot(
                CircuitState(data["state"]), int(data["failures"]), float(data["opened_at"])
            )
        return result
