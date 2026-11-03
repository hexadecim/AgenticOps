"""Fixed-window rate limiter in Redis, behind the `redis` breaker. Fails open: Redis trouble must not lock
everyone out (identity-service's account lockout still protects each account)."""

from __future__ import annotations

import structlog
from opspilot_common.resilience import ResilienceError, Resilient
from redis.asyncio import Redis
from redis.exceptions import RedisError

log = structlog.get_logger(__name__)


class RedisRateLimiter:
    def __init__(self, redis: Redis, resilient: Resilient) -> None:
        self._redis, self._resilient = redis, resilient

    async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        async def count() -> int:
            pipe = self._redis.pipeline()
            pipe.incr(f"opspilot:ratelimit:{key}")
            pipe.expire(f"opspilot:ratelimit:{key}", window_seconds, nx=True)
            hits, _ = await pipe.execute()
            return int(hits)

        try:
            return await self._resilient.call(count) <= limit
        except (ResilienceError, RedisError, OSError) as exc:
            log.warning("rate_limiter_unavailable_fail_open", error=str(exc))
            return True


class AllowAllRateLimiter:
    async def allow(self, key: str, limit: int, window_seconds: int) -> bool:
        return True
