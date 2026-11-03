"""One way to build a service's resilience registry: breaker state in Redis, shared by every replica and
visible system-wide (api-gateway's System health screen reads the same keys)."""

from __future__ import annotations

from collections.abc import Sequence

from redis.asyncio import Redis

from .clock import WallClock
from .config import ResilienceSettings
from .listeners import BreakerListener, LoggingListener, PrometheusListener
from .policy import FailurePolicy, default_is_failure
from .resilient import ResilienceRegistry
from .store import RedisStateStore

BREAKER_KEY_PREFIX = "opspilot:breaker:"


def redis_registry(
    redis: Redis,
    config_path: str | None,
    *,
    listeners: Sequence[BreakerListener] = (),
    is_failure: FailurePolicy = default_is_failure,
) -> ResilienceRegistry:
    settings = ResilienceSettings.from_yaml(config_path) if config_path else ResilienceSettings()
    return ResilienceRegistry(
        settings,
        store=RedisStateStore(redis, prefix=BREAKER_KEY_PREFIX),
        clock=WallClock(),
        listeners=(LoggingListener(), PrometheusListener(), *listeners),
        is_failure=is_failure,
    )
